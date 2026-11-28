"""Point-in-time, once-a-session morning exposure observation for TQQQ and QQQ."""

from functools import lru_cache
from io import StringIO
import math

import pandas as pd
import requests

from tradingagents.dataflows.intraday import (
    IntradayDataError,
    aware_utc,
    completed_minutes,
    fetch_regular_bars,
    market_session,
    utc_now,
)
from tradingagents.dataflows.vendors.yahoo.sessions import NY_TZ, is_session
from tradingagents.extended_context import fetch_context
from tradingagents.intraday import sizing_status
from tradingagents.opening_rule import ORDER_FEE, STRATEGY, opening_target
from tradingagents.portfolio import ReferenceQuote
from tradingagents.risk import validate_allocation


def vxn_inputs(frame, day):
    dates = pd.to_datetime(frame["DATE"], format="%m/%d/%Y")
    closes = pd.Series(pd.to_numeric(frame["CLOSE"], errors="coerce").to_numpy(), index=dates)
    past = closes[closes.index < pd.Timestamp(day)]
    sessions = [
        d
        for d in pd.date_range(end=pd.Timestamp(day) - pd.Timedelta(days=1), periods=45)
        if is_session(d)
    ][-21:]
    if len(sessions) != 21 or past.index.has_duplicates:
        raise ValueError("VXN 日期无法核实")
    sample = past.reindex(sessions)
    if not all(math.isfinite(v) and v > 0 for v in sample):
        raise ValueError("前一交易日或此前 20 个交易日缺少 VXN 收盘值")
    return float(sample.iloc[-1]), float(sample.iloc[:-1].median()), str(sessions[-1].date())


@lru_cache(maxsize=2)
def read_vxn(day):
    url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VXN_History.csv"
    reply = requests.get(url, timeout=8)
    reply.raise_for_status()
    return vxn_inputs(pd.read_csv(StringIO(reply.text)), day)


def pending_sizing(book, limits):
    result = sizing_status(book, limits)
    result["reason"] = (
        "需提供带时区的账户快照、可卖数量和明确的风控参数。"
        if result["missing"]
        else "等待当日有效的目标和同步的账户报价。"
    )
    if not result["missing"]:
        result["status"] = "WAITING"
    return result


def target_sizing(report, book, limits, now):
    pending = pending_sizing(book, limits)
    if book is None or pending["missing"]:
        return pending

    def stop(reason, status="NEEDS_INPUT"):
        return dict(status=status, reason=reason, missing=[], validated_allocation=None)

    if limits.get("fee_per_order") != ORDER_FEE:
        return stop("请按当前策略成本显式配置每单 5 美元。", "BLOCKED")
    if book.as_of_time is None or "positions" not in book.model_fields_set:
        return stop("需要带时区的账户快照时间和明确的持仓清单。")
    quote = report["snapshots"][0]
    symbol = report["ticker"]
    stamp = aware_utc(book.as_of_time)
    bar_end = aware_utc(quote["bar_end"])
    if not (
        0 <= (now - stamp).total_seconds() < 180 and abs((stamp - bar_end).total_seconds()) < 180
    ):
        return stop("账户快照与分钟报价不同步或已过期。", "BLOCKED")
    day = bar_end.tz_convert(NY_TZ).date()
    if stamp.tz_convert(NY_TZ).date() != day or (book.as_of_date and book.as_of_date != day):
        return stop("账户日期与交易日不一致。", "BLOCKED")
    holdings = book.positions_in(symbol)
    if any(p.available_quantity is None or p.available_quantity > p.quantity for p in holdings):
        return stop("缺少有效的可卖股数，不能按总持仓推算。")
    ref = ReferenceQuote(
        ticker=symbol,
        price=quote["price"],
        currency="USD",
        as_of_date=day,
        price_basis="unadjusted",
    )
    snapshot = book.model_copy(update={"as_of_date": day, "reference_quotes": [ref]})
    target = report["opening"]["target"]
    if report["action"] == "WAIT":
        return stop("策略目标未变，本轮没有调仓信号。", "NO_CHANGE")
    state = dict(
        company_of_interest=symbol,
        trade_date=day.isoformat(),
        asset_type="etf",
        portfolio_data=snapshot.model_dump(),
        final_rating="Underweight" if report["action"] == "WATCH_RISK" else "Buy",
        allocation_proposal=dict(
            target_weight=target, stop_loss=book.intraday_stop_prices.get(symbol)
        ),
    )
    checked = validate_allocation(state, limits)
    allocation = checked["validated_allocation"]
    held = sum(p.quantity for p in holdings)
    available = sum(p.available_quantity for p in holdings)
    if allocation and -allocation["delta_quantity"] > available:
        return stop("拟减持股数超过账户可卖股数。", "BLOCKED")
    return dict(
        status=checked["status"],
        reason="；".join(checked["reasons"]) or "已按提供的快照和风控规则核对。",
        missing=[],
        validated_allocation=allocation,
        held=held,
        available=available,
    )


class OpeningEngine:
    def __init__(
        self,
        ticker,
        book,
        *,
        clock=utc_now,
        bars=fetch_regular_bars,
        vxn=read_vxn,
        premarket=fetch_context,
    ):
        self.ticker, self.book = ticker, book
        self.clock, self.bars, self.vxn, self.premarket = clock, bars, vxn, premarket
        self.context = {}

    def reset_observations(self):
        self.context.clear()

    def step(self, *, portfolio=None, limits=None):
        now = aware_utc(self.clock())
        local = now.tz_convert(NY_TZ)
        day = str(local.date())
        cutoff = local.normalize() + pd.Timedelta(hours=9, minutes=35)
        expiry = cutoff + pd.Timedelta(minutes=1)
        report = dict(
            version="intraday-v1",
            strategy_id=STRATEGY,
            ticker=self.ticker,
            generated_at=now.isoformat(),
            session=market_session(now),
            status="WAITING",
            action="WAIT",
            snapshots=[],
            model_status="RULE_ONLY",
            valid_until=None,
            reason="等待美东 09:35 的五根完整分钟线。",
            sizing=pending_sizing(portfolio, limits),
            limitations=["09:35 目标仅用于研究，行情可能延迟。"],
        )
        saved = self.book.opening_decision(STRATEGY, self.ticker, day)
        if saved:
            report["opening"] = saved["opening"]
            if cutoff <= local < expiry:
                report.update(saved, status="READY")
                report["sizing"] = target_sizing(report, portfolio, limits, now)
            else:
                report["reason"] = "当日 09:35 决策已存档，窗口已结束。"
            return report
        if report["session"] != "REGULAR":
            report["reason"] = "当前不在常规交易时段。"
            return report
        if local >= expiry:
            report["reason"] = "今天的 09:35 决策窗口已结束，不补发信号。"
            return report
        try:
            previous, reference, vxn_day = self.vxn(day)
            if day not in self.context or (
                self.context[day]["status"] != "AVAILABLE" and local >= cutoff
            ):
                try:
                    data = self.premarket(day)
                    pre = data["premarket"] if data["date"] == day else {}
                except Exception:
                    pre = {}
                self.context = {
                    day: pre
                    if pre.get("direction") in ("WEAK", "STRONG", "MIXED")
                    and pre.get("status") == "AVAILABLE"
                    else {
                        "status": "UNAVAILABLE",
                        "direction": "MISSING",
                        "reason": "盘前缺失，沿用常规规则。",
                    }
                }
            if local < cutoff:
                return report
            for ticker in dict.fromkeys((self.ticker, "TQQQ", "QQQ")):
                frame = self.bars(ticker)
                if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
                    raise ValueError("分钟行情缺少时区")
                values = completed_minutes(ticker, frame.loc[frame.index < cutoff], now, minimum=5)
                if len(values) != 5 or values.index[-1] + pd.Timedelta(minutes=1) != cutoff:
                    raise ValueError("开盘分钟线不同步")
                report["snapshots"].append(
                    dict(
                        ticker=ticker, price=float(values.Close.iloc[-1]), bar_end=stamp_utc(cutoff)
                    )
                )
            pre = self.context[day]
            old = self.book.prior_target(STRATEGY, self.ticker, day)
            target = opening_target(
                old, previous, reference, pre["direction"] in ("WEAK", "MISSING")
            )
            known = aware_utc(self.clock())
            if known >= expiry or known < now:
                report["reason"] = "行情到齐时已错过决策窗口。"
                return report
            report.update(
                status="READY",
                action="WATCH_RISK" if target < old else "WATCH_LONG" if target > old else "WAIT",
                generated_at=known.isoformat(),
                valid_until=stamp_utc(expiry),
                opening=dict(
                    previous=old,
                    target=target,
                    vxn=previous,
                    median=reference,
                    vxn_date=vxn_day,
                    pre=pre["direction"],
                ),
                premarket_context={"premarket": pre},
                reason=f"前日 VXN {previous:g}，此前 20 日中位数 {reference:g}，盘前 {pre['direction']}。"
                f"规则目标由 {old:.0%} 到 {target:.0%}。",
            )
            report.update(self.book.opening_decision(STRATEGY, self.ticker, day, report))
            report["sizing"] = target_sizing(report, portfolio, limits, known)
        except Exception as exc:
            reason = (
                str(exc)
                if type(exc) is ValueError
                and str(exc)
                in {
                    "VXN 日期无法核实",
                    "前一交易日或此前 20 个交易日缺少 VXN 收盘值",
                    "分钟行情缺少时区",
                    "开盘分钟线不同步",
                }
                else type(exc).__name__
            )
            if isinstance(exc, IntradayDataError):
                reason = str(exc)
            report.update(
                action="WAIT",
                status="UNAVAILABLE",
                valid_until=None,
                reason=f"09:35 输入未通过核验：{reason}。",
            )
        return report


def stamp_utc(value):
    return aware_utc(value).isoformat()
