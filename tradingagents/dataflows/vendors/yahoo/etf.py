"""ETF research inputs, with bounded current-only constituent lookups.

Uses the installed/public yfinance API (FundsData.top_holdings,
Ticker.get_earnings_estimate/get_eps_trend/get_earnings_dates):
https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.html
QQQ holdings are a research proxy for Nasdaq-100 exposure, NOT TQQQ's swaps,
cash portfolio, exact index weights, or a historical holdings archive.
"""
from datetime import UTC, datetime
import math
import re

import pandas as pd
import pytz
import yfinance as yf

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.vendors.yahoo.common import yf_retry
from tradingagents.dataflows.vendors.yahoo.sessions import is_session, is_us_equity
from tradingagents.dataflows.vendors.yahoo.snapshot import build_verified_market_snapshot


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _now_utc():
    return datetime.now(UTC)


def _text(value):
    return str(value).replace("|", "/").replace("\n", " ") if value is not None and not pd.isna(value) else "unavailable"


def _numeric(value):
    number = _number(value)
    return f"{number:.4g}" if number is not None else "unavailable"


def _fetch(call, label, missing):
    try:
        return yf_retry(call, max_retries=1, base_delay=1.0)
    except VendorError as exc:
        missing.append(f"{label}: unavailable ({type(exc).__name__})")
        return None


def simulate_daily_leverage(daily_returns, leverage):
    """Illustrative compounding, before fees, funding and tracking error."""
    if _number(leverage) is None:
        raise ValueError("leverage must be finite")
    underlying, leveraged = 1.0, 1.0
    for daily in daily_returns:
        daily = _number(daily)
        if daily is None or daily <= -1 or 1 + leverage * daily <= 0:
            raise ValueError("scenario exceeds valid daily return/leverage bounds")
        underlying *= 1 + daily
        leveraged *= 1 + leverage * daily
    return underlying - 1, leveraged - 1


def _scenarios(profile):
    if not profile.daily_reset or profile.daily_leverage is None:
        return "Daily leveraged-return scenarios: not applicable or leverage unverified."
    lines = ["### Illustrative daily-reset paths (NOT forecasts)", "",
             "Before fees, financing and tracking error; no current price target is implied.",
             "",
             "| Hypothetical 10-session path | Index cumulative return | Daily-reset fund return |",
             "| --- | ---: | ---: |"]
    for label, path in (("+1% each day", [0.01] * 10), ("-1% each day", [-0.01] * 10),
                        ("alternating +2% / -2%", [0.02, -0.02] * 5)):
        index, fund = simulate_daily_leverage(path, profile.daily_leverage)
        lines.append(f"| {label} | {index:+.2%} | {fund:+.2%} |")
    lines.extend(["", "A stop price cannot cap losses from gaps/slippage. Product structure alone is not a bearish signal."])
    return "\n".join(lines)


def _holdings(proxy, missing):
    data = _fetch(lambda: yf.Ticker(proxy).get_funds_data().top_holdings,
                  f"{proxy} holdings", missing)
    if not isinstance(data, pd.DataFrame) or data.empty or "Holding Percent" not in data:
        missing.append(f"{proxy} holdings: no usable holdings/weight snapshot; no membership inferred")
        return []
    selected = []
    seen = set()
    for symbol, row in data.iterrows():
        symbol = str(symbol).strip().upper()
        weight = _number(row.get("Holding Percent"))
        if symbol in seen or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", symbol) or weight is None or not 0 < weight <= 1:
            continue
        selected.append({"ticker": symbol, "name": _text(row.get("Name")), "weight": weight})
        seen.add(symbol)
    if sum(row["weight"] for row in selected) > 1.00001:
        missing.append(f"{proxy} holdings: invalid total weight; snapshot rejected")
        return []
    return sorted(selected, key=lambda row: row["weight"], reverse=True)[:10]


def _earnings(symbol, weight, start_day, end_day, missing):
    """Latest consensus snapshots + upcoming vendor dates, not confirmed IR dates."""
    company = yf.Ticker(symbol)
    estimates = _fetch(lambda: company.get_earnings_estimate(), f"{symbol} EPS estimates", missing)
    trends = _fetch(lambda: company.get_eps_trend(), f"{symbol} EPS revisions", missing)
    dates = _fetch(lambda: company.get_earnings_dates(limit=12), f"{symbol} earnings calendar", missing)
    lines = [f"#### {symbol} (proxy weight {weight:.2%})",
             "| Fiscal period | EPS consensus | EPS YoY (positive prior base) | EPS revision vs 30 days ago | Currency | Methodology |",
             "| --- | ---: | ---: | ---: | --- | --- |"]
    count = 0
    for period in ("0q", "+1q"):
        if not isinstance(estimates, pd.DataFrame) or period not in estimates.index:
            continue
        row = estimates.loc[period]
        if not isinstance(row, pd.Series):
            continue
        avg, previous = _number(row.get("avg")), _number(row.get("yearAgoEps"))
        if avg is None:
            missing.append(f"{symbol} {period}: EPS consensus value unavailable")
        growth = f"{avg / previous - 1:+.2%}" if avg is not None and previous is not None and previous > 0 else "unavailable / nonpositive base"
        revision = "unavailable"
        if isinstance(trends, pd.DataFrame) and period in trends.index:
            trend = trends.loc[period]
            if isinstance(trend, pd.Series):
                current, old = _number(trend.get("current")), _number(trend.get("30daysAgo"))
                comparable = all(not (isinstance(row.get(key), str) and isinstance(trend.get(key), str)
                                      and row.get(key).strip() and trend.get(key).strip()
                                      and row.get(key) != trend.get(key))
                                 for key in ("currency", "methodology"))
                if comparable and current is not None and old is not None and old > 0:
                    revision = f"{current / old - 1:+.2%} (trend current {_numeric(current)})"
                elif not comparable:
                    revision = "unavailable: currency/methodology mismatch"
        lines.append(f"| {period} | {_numeric(avg)} | {growth} | {revision} | {_text(row.get('currency'))} | {_text(row.get('methodology'))} |")
        count += 1
    if not count:
        missing.append(f"{symbol}: current/next-quarter EPS consensus unavailable")
    upcoming = []
    if isinstance(dates, pd.DataFrame):
        for value in dates.index:
            try:
                stamp = pd.Timestamp(value)
            except (ValueError, TypeError):
                continue
            if pd.isna(stamp):
                continue
            if stamp.tzinfo is None:
                label, day = f"{stamp.date()} (time zone/time unverified)", stamp.date()
            else:
                if is_us_equity(symbol):
                    stamp = stamp.tz_convert("America/New_York")
                if stamp.tz_convert("UTC") < pd.Timestamp(_now_utc()):
                    continue
                label, day = stamp.isoformat(), stamp.date()
            if start_day <= day <= end_day:
                upcoming.append(label)
    lines.append("Upcoming earnings (vendor schedule; confirm with company IR): " + (
        "; ".join(sorted(set(upcoming))) if upcoming else "no dated event retrieved in the research window; not proof that no event is scheduled."
    ))
    if not isinstance(dates, pd.DataFrame) or dates.empty:
        missing.append(f"{symbol}: earnings schedule unavailable")
    return "\n".join(lines)


def build_etf_research(profile, trade_date):
    """Fetch bounded evidence once, before the ETF analyst's tool-free summary."""
    if profile.kind != "etf":
        raise ValueError("ETF research requires an ETF instrument profile")
    config = get_config()
    limit = max(1, min(10, int(config.get("etf_research_constituents", 5))))
    horizon = max(1, min(10, int(config.get("etf_research_horizon_sessions", 10))))
    proxy = profile.research_proxy or profile.ticker
    lines = [f"## ETF research inputs for {profile.ticker}", f"- Analysis date: {trade_date}",
             f"- Underlying index: {profile.underlying_index or 'unverified'}",
             f"- Research proxy: {proxy}; execution instrument remains {profile.ticker}.",
             f"- Structural source: {profile.source or 'vendor type / explicit profile; terms unverified'}",
             "- Distinguish fund structure, dated observations and hypothetical scenarios.",
             "- Fund expense ratio, current financing rate, derivatives/collateral exposure and premium/discount are unverified; do not invent them."]
    missing = []
    try:
        lines.extend(["### Research-proxy market evidence", build_verified_market_snapshot(proxy, trade_date, look_back_days=5)])
    except VendorError as exc:
        missing.append(f"{proxy} price/indicators: unavailable ({type(exc).__name__})")
    lines.append(_scenarios(profile))
    if is_historical(trade_date):
        lines.append("Current holdings, EPS consensus/revisions and upcoming earnings are withheld: the vendor has no historical publication vintage. No current constituent membership or weight is inserted into this past run.")
    else:
        lines.append(f"- Current-only snapshots fetched at {_now_utc().isoformat()}; holdings reported-as-of date is unavailable, so retrieval time is not a holdings effective date.")
        holdings = _holdings(proxy, missing)
        if holdings:
            lines.extend(["### Proxy top holdings (partial coverage)",
                          (f"These are {proxy}'s reported holdings, not {profile.ticker}'s derivative holdings or exact index weights."
                           if proxy != profile.ticker else "These are the fund's reported top holdings, not a complete holdings or exact index-weight archive.")])
            table = ["| Ticker | Name | Proxy weight |", "| --- | --- | ---: |"]
            for row in holdings:
                table.append(f"| {row['ticker']} | {row['name']} | {row['weight']:.2%} |")
            lines.append("\n".join(table))
            lines.append(f"Listed weight coverage: {sum(row['weight'] for row in holdings):.2%}; weights are not renormalized. Do not average EPS growth into an index-profit forecast.")
            start = min(pd.Timestamp(trade_date).date(), _now_utc().astimezone(pytz.timezone('America/New_York')).date())
            days, cursor = [], pd.Timestamp(start)
            for _ in range(30):
                if is_session(cursor):
                    days.append(cursor.date())
                if len(days) == horizon:
                    break
                cursor += pd.Timedelta(days=1)
            if len(days) != horizon:
                raise ValueError("Could not form the ETF research-session window")
            lines.append(f"### Constituent catalysts: {days[0]} through {days[-1]} ({horizon} scheduled US sessions)")
            lines.append(f"Coverage: at most the {limit} largest retrieved proxy holdings. Fiscal 0q/+1q EPS are quarterly estimates, not one-week profits; constituent EPS growth is not a share-price forecast. Other constituents may have material events.")
            if is_us_equity(proxy):
                for row in holdings[:limit]:
                    lines.append(_earnings(row['ticker'], row['weight'], days[0], days[-1], missing))
            else:
                lines.append("The US session dates above are only a reference. The proxy's exchange calendar is unverified, so constituent-event-window analysis is withheld.")
    if missing:
        lines.extend(["### Missing or limited evidence", *[f"- {item}" for item in missing]])
    lines.append("Macro checklist: CPI/PCE, FOMC and Treasury-yield changes can affect the index. Read the News Analyst's dated US macro release evidence for event times, consensus coverage and snapshot gaps. This ETF packet alone supplies no verified economic-release calendar; if the News Analyst is not selected or its evidence is unavailable, event dates/consensus remain unverified.")
    return "\n\n".join(lines)
