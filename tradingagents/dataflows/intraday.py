"""Unadjusted, completed US regular-session minute bars; no daily-cache fallback.

Yahoo Finance is a best-effort source, not an entitled broker real-time feed.
Bar timestamps are interval STARTS. Freshness is measured from their END.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.sessions import NY_TZ, is_session, is_us_equity, session_close

MAX_BAR_AGE_SECONDS = 180
MIN_BARS = 21


class IntradayDataError(ValueError):
    """A safe diagnostic authored here, rather than raw provider error text."""


def utc_now():
    return pd.Timestamp.now(tz="UTC")


def aware_utc(value):
    value = pd.Timestamp(value)
    if pd.isna(value) or value.tzinfo is None:
        raise IntradayDataError("需要带时区的有效时间戳")
    return value.tz_convert("UTC")


def market_session(now):
    ny = aware_utc(now).tz_convert(NY_TZ)
    day = ny.tz_localize(None).normalize()
    if not is_session(day):
        return "CLOSED"
    opened = day.tz_localize(NY_TZ) + pd.Timedelta(hours=9, minutes=30)
    if ny < opened:
        return "PREMARKET"
    return "REGULAR" if ny < session_close(day) else "CLOSED"


@dataclass(frozen=True)
class MinuteSnapshot:
    ticker: str
    bar_start: str
    bar_end: str
    fetched_at: str
    price: float
    vwap: float
    ema9: float
    ema20: float
    momentum5_pct: float
    session_open: float
    session_high: float
    session_low: float
    recent_high: float
    recent_low: float
    volume: float
    bars: int
    source: str = "Yahoo Finance / yfinance / 1m; potentially delayed; unadjusted"

    def to_dict(self):
        return asdict(self)


def snapshot_from_bars(ticker, frame, now):
    """Reject malformed/gapped sessions, stale tails and unknown time zones.

    No interpolation, forward filling, using an unfinished candle, or moving
    a source timestamp forward because a fetch succeeded.
    """
    symbol, now = normalize_symbol(ticker), aware_utc(now)
    if not is_us_equity(symbol):
        raise IntradayDataError("盘中模式当前只支持美股／美股 ETF 普通代码")
    if market_session(now) != "REGULAR":
        raise IntradayDataError("当前不在美股常规交易时段")
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise IntradayDataError("分钟行情为空，可能被限流或尚无数据")
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise IntradayDataError("分钟行情缺少明确时区")
    required = ["Open", "High", "Low", "Close", "Volume"]
    if any(c not in frame.columns for c in required):
        raise IntradayDataError("分钟行情字段不完整")
    data = frame[required].copy()
    data.index = data.index.tz_convert("UTC")
    if data.index.has_duplicates or (data.index != data.index.floor("min")).any():
        raise IntradayDataError("分钟行情重复或时间边界不明确")
    if (data.index > now.floor("min")).any():
        raise IntradayDataError("行情含未来时间戳")
    ny = now.tz_convert(NY_TZ)
    day = ny.tz_localize(None).normalize()
    start = (day.tz_localize(NY_TZ) + pd.Timedelta(hours=9, minutes=30)).tz_convert("UTC")
    end = session_close(day).tz_convert("UTC")
    # Exclude pre/post-market and the active (incomplete) minute.
    data = data.loc[(data.index >= start) & (data.index < end)
                    & (data.index + pd.Timedelta(minutes=1) <= now)].sort_index()
    if len(data) < MIN_BARS:
        raise IntradayDataError("本交易日已完成分钟线不足 21 根，等待指标形成")
    values = data.apply(pd.to_numeric, errors="coerce")
    if not all(math.isfinite(float(v)) for v in values.to_numpy().flat):
        raise IntradayDataError("分钟行情含无效数值")
    prices = values[["Open", "High", "Low", "Close"]]
    if ((prices <= 0).any().any() or (values.Volume < 0).any()
            or (values.High < prices.max(axis=1)).any()
            or (values.Low > prices.min(axis=1)).any()):
        raise IntradayDataError("分钟行情价格或成交量不合理")
    expected = pd.date_range(start, values.index[-1], freq="min")
    if not values.index.equals(expected):
        raise IntradayDataError("本交易日分钟线缺口，无法验证日内 VWAP；等待完整数据")
    bar_end = values.index[-1] + pd.Timedelta(minutes=1)
    if (now - bar_end).total_seconds() > MAX_BAR_AGE_SECONDS:
        raise IntradayDataError("分钟行情已过期（超过 180 秒），暂停判断")
    volume = float(values.Volume.sum())
    if volume <= 0:
        raise IntradayDataError("本交易日没有有效成交量")
    typical = (values.High + values.Low + values.Close) / 3
    snapshot = MinuteSnapshot(
        ticker=symbol, bar_start=values.index[-1].isoformat(), bar_end=bar_end.isoformat(),
        fetched_at=now.isoformat(), price=float(values.Close.iloc[-1]),
        vwap=float((typical * values.Volume).sum() / volume),
        ema9=float(values.Close.ewm(span=9, adjust=False).mean().iloc[-1]),
        ema20=float(values.Close.ewm(span=20, adjust=False).mean().iloc[-1]),
        momentum5_pct=float((values.Close.iloc[-1] / values.Close.iloc[-6] - 1) * 100),
        session_open=float(values.Open.iloc[0]), session_high=float(values.High.max()),
        session_low=float(values.Low.min()), recent_high=float(values.High.iloc[-6:-1].max()),
        recent_low=float(values.Low.iloc[-6:-1].min()), volume=volume, bars=len(values),
    )
    if not all(math.isfinite(v) for v in snapshot.to_dict().values() if isinstance(v, (int, float))):
        raise IntradayDataError("指标计算溢出，暂停判断")
    return snapshot


def fetch_snapshot(ticker, *, clock=utc_now):
    symbol = normalize_symbol(ticker)
    if not is_us_equity(symbol):
        raise IntradayDataError("盘中模式当前只支持美股／美股 ETF 普通代码")
    obj = yf.Ticker(symbol)
    frame = obj.history(period="1d", interval="1m", prepost=False, actions=False,
                        auto_adjust=False, back_adjust=False, repair=False,
                        keepna=True, timeout=10)
    meta = obj.history_metadata
    if (meta.get("symbol", "").upper() != symbol or meta.get("currency") != "USD"
            or meta.get("exchangeTimezoneName") != NY_TZ
            or meta.get("instrumentType") not in {"EQUITY", "ETF"}):
        raise IntradayDataError("无法核实代码、USD 币种及美股品种身份")
    return snapshot_from_bars(symbol, frame, clock())


def freshness_error(snapshots, now):
    now = aware_utc(now)
    if market_session(now) != "REGULAR":
        return "当前不在美股常规交易时段，等待开盘"
    for snap in snapshots:
        age = (now - aware_utc(snap.bar_end)).total_seconds()
        if age < 0 or age >= MAX_BAR_AGE_SECONDS:
            return f"{snap.ticker} 分钟行情已过期或时间异常，暂停判断"
        if aware_utc(snap.bar_end).tz_convert(NY_TZ).date() != now.tz_convert(NY_TZ).date():
            return f"{snap.ticker} 行情不是当前交易日"
    return None
