# Modified for KY TradingAgent 0.0.1 (2026-11-23).
import logging
import os
import time

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.symbols import normalize_symbol, safe_ticker_component
from tradingagents.dataflows.vendors.yahoo.common import raise_for_empty, yf_retry
from tradingagents.dataflows.vendors.yahoo.sessions import (
    PUBLICATION_BUFFER,
    is_us_equity,
    is_session,
    latest_completed_session,
    session_close,
)

logger = logging.getLogger(__name__)

# A vendor's latest OHLCV row this many calendar days before the requested date
# is treated as stale. Generous enough to span long holiday weekends, tight
# enough to catch the year-old frames yfinance occasionally returns (#1021).
MAX_OHLCV_STALE_DAYS = 10

# How long a same-day cache that does not yet reach the requested day may be
# reused before it is refetched (#1150). Short enough that an intraday run picks
# up today's close soon after it publishes, long enough that a day with no bar
# at all (weekend, holiday) cannot trigger a download on every call.
OHLCV_CACHE_TTL_SECONDS = 900
# A single analyst may request many indicators. Once a valid fallback has been
# selected, do not repeat four failing requests for each indicator in that run.
OHLCV_REFRESH_COOLDOWN_SECONDS = 60
_refresh_cooldowns: dict[str, float] = {}


def _ensure_date_column(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize the date column to ``Date``.

    Some yfinance builds leave the index unnamed (so ``reset_index()`` yields
    ``index``) or use ``Datetime`` for intraday data. Rename the first
    date-like column so indicators don't silently drop when it isn't ``Date``.
    """
    if "Date" in data.columns:
        return data
    for candidate in ("index", "Datetime", "date"):
        if candidate in data.columns:
            return data.rename(columns={candidate: "Date"})
    return data


def _local_midnight(value) -> pd.Timestamp:
    """A single timestamp as its naive, midnight-normalized local date (or NaT)."""
    if pd.isna(value):
        return pd.NaT
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError):
        return pd.NaT
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)  # drop tz, keep the local wall-clock date
    return ts.normalize()


def _normalize_dates(dates) -> pd.Series:
    """Parse to naive, midnight-normalized dates so tz-aware or intraday
    timestamps compare correctly against the naive ``as_of_date`` cutoff (#1201).

    Normalized per element: 5 years of yfinance bars span daylight-saving
    changes (and cache CSVs round-trip the offsets as strings), so the series can
    carry mixed UTC offsets that ``pd.to_datetime`` cannot unify without
    ``utc=True`` — which would shift non-US (positive-offset) markets to the
    previous day. Keeping each bar's own local date avoids both.
    """
    return pd.to_datetime(pd.Series(dates).map(_local_midnight))


def _clean_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize a stock DataFrame for stockstats: parse/normalize dates and
    coerce prices to numeric (NaN where invalid). Dropping incomplete rows and
    filling gaps is left to ``_fill_price_gaps`` so the caller can first inspect
    the latest in-range bar (#1201)."""
    data = _ensure_date_column(data)
    if "Date" not in data or "Close" not in data:
        raise ValueError("OHLCV requires Date and Close columns")
    data["Date"] = _normalize_dates(data["Date"])
    data = data.dropna(subset=["Date"]).copy()

    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data[price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")
    data[price_cols] = data[price_cols].replace([float("inf"), -float("inf")], float("nan"))
    data.loc[data["Close"] <= 0, "Close"] = float("nan")
    return data.sort_values("Date").drop_duplicates("Date", keep="last").reset_index(drop=True)


def _fill_price_gaps(data: pd.DataFrame) -> pd.DataFrame:
    """Drop rows with no close and forward/back-fill remaining price gaps so
    indicators compute on a continuous series."""
    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    # copy() so a filtered (sliced) input is written to safely, not via a view.
    data = data.dropna(subset=["Close"]).copy()
    data[price_cols] = data[price_cols].ffill().bfill()
    return data


def _coerce_ohlcv_dates(data: pd.DataFrame) -> pd.Series:
    """Return parsed dates from an OHLCV frame, whether Date is a column or the index."""
    if "Date" in data.columns:
        return pd.to_datetime(data["Date"], errors="coerce").dropna()
    # yfinance keeps the dates in the index (a DatetimeIndex, sometimes unnamed).
    if isinstance(data.index, pd.DatetimeIndex):
        return pd.Series(pd.to_datetime(data.index, errors="coerce")).dropna()
    # Fallback: expose the index and look for any date-like column.
    df = data.reset_index()
    for col in ("Date", "Datetime", "date", "index"):
        if col in df.columns:
            parsed = pd.to_datetime(df[col], errors="coerce").dropna()
            if not parsed.empty:
                return parsed
    return pd.Series(dtype="datetime64[ns]")


def _assert_ohlcv_not_stale(
    data: pd.DataFrame,
    as_of_date: str,
    symbol: str,
    canonical: str | None = None,
    *,
    max_stale_days: int = MAX_OHLCV_STALE_DAYS,
) -> None:
    """Reject OHLCV whose latest row is far older than as_of_date.

    Raises NoMarketDataError (with a stale-specific detail) so the router treats
    it like any other "no usable data from this vendor" — try the next vendor,
    then emit one clear unavailable signal. Empty frames are left to the
    caller's existing no-data handling; this guards only the dangerous case of
    present-but-stale rows (a vendor returning a year-old frame that would
    otherwise feed wrong prices to the agent, #1021).
    """
    if data is None or data.empty:
        return
    requested = pd.to_datetime(as_of_date, errors="coerce")
    if pd.isna(requested):
        return
    requested = requested.normalize()
    dates = _coerce_ohlcv_dates(data)
    if dates.empty:
        return
    latest = dates.max().normalize()
    stale_days = (requested - latest).days
    if stale_days > max_stale_days:
        raise NoMarketDataError(
            symbol,
            canonical,
            f"latest row is {latest.date()}, {stale_days} days before the "
            f"requested {requested.date()} (stale) — refusing to use it",
        )


def _cache_is_fresh(data_file, as_of_dt, now) -> bool:
    """Whether the symbol's cached download can serve this request.

    The file holds the download made on the day it was written, so it serves
    only that day. A current-day request also refetches once the file is older
    than the TTL: Yahoo publishes a partial daily candle during market hours,
    whose ``Close`` is not the closing price, and row inspection cannot tell it
    from a final one (#1150).
    """
    written = pd.Timestamp.fromtimestamp(os.path.getmtime(data_file))
    if written > now:
        return False
    if written.date() != now.date():
        return False
    return as_of_dt.date() < now.date() or (now - written).total_seconds() <= OHLCV_CACHE_TTL_SECONDS


def _utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _fallback_rows(cached, data_file, as_of_dt, symbol, canonical, config, now_utc):
    """A failed refresh may reuse only a demonstrably completed US session.

    File age alone is not proof: yesterday's CSV could contain a candle saved
    before the closing bell. Require coverage of the expected last session AND
    a download time after its close, and never carry its partial tail forward.
    Other exchanges need an explicit supported calendar before using fallback.
    """
    if not is_us_equity(canonical, config):
        raise NoMarketDataError(symbol, canonical, "cache fallback has no supported exchange calendar")
    expected = latest_completed_session(as_of_dt, now_utc)
    rows = _clean_dataframe(cached.copy())
    rows = rows[(rows["Date"] <= expected) & rows["Close"].notna()].copy()
    if rows.empty or rows["Date"].iloc[-1] != expected:
        raise NoMarketDataError(
            symbol, canonical, f"cache does not cover the last completed session {expected.date()}"
        )
    written = pd.Timestamp.fromtimestamp(os.path.getmtime(data_file), tz="UTC")
    if written > now_utc or written < session_close(expected).tz_convert("UTC") + PUBLICATION_BUFFER:
        raise NoMarketDataError(symbol, canonical, "cache was not saved after the session completed")
    return rows


def describe_data_source(data: pd.DataFrame) -> str:
    """Readable provenance shared by price tools and the verified snapshot."""
    source = data.attrs.get("market_data", {})
    if not source:
        return ""
    lines = [
        f"- Data source: {source['source']}",
        f"- Latest data row: {source['latest_row']}",
        "- Price basis: Yahoo auto-adjusted daily OHLCV (splits/distributions).",
    ]
    if not source.get("historical"):
        lines.insert(1, f"- Data fetched at (UTC): {source['fetched_at']}")
    if source.get("last_completed_session"):
        lines.append(f"- Last completed scheduled US session: {source['last_completed_session']}")
    if source.get("provisional"):
        lines.append("- WARNING: latest row is provisional or not a completed scheduled session; it is not a final closing price.")
    if source.get("missing_completed_session"):
        lines.append("- WARNING: data does not yet cover the last completed session; do not treat this as a current quote.")
    if source.get("refresh_failed"):
        lines.append("- WARNING: live refresh failed; using validated cached daily bars, not a live quote.")
    if source.get("historical_reference"):
        lines.insert(0, "- HISTORICAL REFERENCE ONLY: latest completed session is missing; prices and indicators below describe an older session, not the current market. Do not use them as current execution/sizing inputs.")
    return "\n".join(lines) + "\n"


def _historical_reference_rows(cached, data_file, as_of_dt, symbol, canonical, config, now_utc):
    """An older completed prefix can support research, never a current quote.

    A candle saved intraday never becomes final just because time passes.
    Bound the usable prefix by the session completed when it was downloaded.
    Keep the existing maximum calendar-age guard and exchange restriction.
    """
    if not is_us_equity(canonical, config):
        raise NoMarketDataError(symbol, canonical, "historical cache has no supported exchange calendar")
    written = pd.Timestamp.fromtimestamp(os.path.getmtime(data_file), tz="UTC")
    if written > now_utc:
        raise NoMarketDataError(symbol, canonical, "cache timestamp is in the future")
    certified = latest_completed_session(as_of_dt, written)
    rows = _clean_dataframe(cached.copy())
    rows = rows[(rows["Date"] <= certified) & rows["Close"].notna()].copy()
    rows = rows[rows["Date"].map(is_session)].copy()
    if rows.empty:
        raise NoMarketDataError(symbol, canonical, "no certified completed cached row")
    _assert_ohlcv_not_stale(rows, str(as_of_dt.date()), symbol, canonical)
    return rows


def load_ohlcv(symbol: str, as_of_date: str, fill_gaps: bool = True,
               start_date: str | None = None) -> pd.DataFrame:
    """Fetch OHLCV data with caching, filtered to prevent look-ahead bias.

    Downloads 5 years of data up to today and caches per symbol. On
    subsequent calls the cache is reused. Rows after as_of_date are
    filtered out so backtests never see future prices.

    ``fill_gaps`` carries prices forward over gaps so indicators compute on a
    continuous series. Pass ``False`` to read the values as the vendor reported
    them, leaving a cell that was never reported empty.
    """
    # Resolve broker/forex symbols (XAUUSD+ -> GC=F) to Yahoo's convention,
    # then reject values that would escape the cache directory when
    # interpolated into the cache filename (e.g. ``../../tmp/x``).
    canonical = normalize_symbol(symbol)
    safe_symbol = safe_ticker_component(canonical)

    config = get_config()
    as_of_dt = pd.to_datetime(as_of_date).normalize()

    # One cache file per symbol, holding the latest 5y-to-today download.
    now = pd.Timestamp.today()
    now_utc = _utc_now()
    download_start = now - pd.DateOffset(years=5)
    if start_date is not None:
        download_start = min(download_start, pd.Timestamp(start_date))
    start_str = download_start.strftime("%Y-%m-%d")
    # yfinance ``end`` is EXCLUSIVE; request tomorrow so today's row is included
    # when as_of_date is the current day (#986). Look-ahead is still prevented by
    # the as_of_date filter below.
    end_str = (now + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    os.makedirs(config["data_cache_dir"], exist_ok=True)
    data_file = os.path.join(
        config["data_cache_dir"],
        f"{safe_symbol}-YFin-data.csv",
    )

    # A cached file may be empty if a prior fetch failed (unknown symbol,
    # transient rate limit). Treat an empty/columnless cache as a miss and
    # re-fetch rather than serving the poisoned file forever.
    data = None
    cached = None
    source = "Yahoo Finance live download"
    refresh_failed = False
    historical_reference = False
    if os.path.exists(data_file):
        try:
            cached = pd.read_csv(data_file, on_bad_lines="skip", encoding="utf-8")
            cached = _clean_dataframe(cached)
        except (OSError, UnicodeError, ValueError, pd.errors.ParserError, pd.errors.EmptyDataError):
            logger.warning("Ignoring unreadable OHLCV cache for %s", canonical)
            cached = None
        # A confirmed last close remains useful overnight/weekends even after
        # the intraday TTL. Do not hit a rate-limited vendor just to rediscover
        # that the exchange has not opened. Keep historical requests unchanged.
        ny = now_utc.tz_convert("America/New_York")
        day = ny.normalize().tz_localize(None)
        outside_regular_session = (not is_session(day) or ny < ny.normalize() + pd.Timedelta(hours=9, minutes=30)
                                   or ny >= session_close(day) + PUBLICATION_BUFFER)
        if (cached is not None and config.get("market_history_reference_fallback", False)
                and as_of_dt.date() >= ny.date() and outside_regular_session):
            try:
                complete = _fallback_rows(cached, data_file, as_of_dt, symbol, canonical, config, now_utc)
                if start_date is None or complete["Date"].min() <= pd.Timestamp(start_date):
                    data, source = complete, "Yahoo Finance completed-session cache (market closed)"
            except (NoMarketDataError, ValueError):
                pass
        if data is None and cached is not None and not cached.empty and _cache_is_fresh(data_file, as_of_dt, now):
            # A current file can still contain old rows. Recheck session
            # coverage for current US requests instead of trusting its mtime.
            covers = True
            if is_us_equity(canonical, config) and as_of_dt.date() >= now_utc.tz_convert("America/New_York").date():
                expected = latest_completed_session(as_of_dt, now_utc)
                valid_dates = cached.loc[cached["Close"].notna() & (cached["Date"] <= as_of_dt), "Date"]
                covers = not valid_dates.empty and valid_dates.max() >= expected
            if start_date is not None and cached["Date"].min() > pd.Timestamp(start_date):
                covers = False
            if start_date is not None:
                in_window = cached[(cached["Date"] >= pd.Timestamp(start_date)) & (cached["Date"] <= as_of_dt) & cached["Close"].notna()]
                if in_window.empty:
                    covers = False
            if covers:
                data = cached
                source = "Yahoo Finance local cache"

    if data is None:
        # yf.download catches every error, a rate limit included, and returns
        # an empty frame. Ticker.history raises the rate limit, so it is retried.
        cooling_down = cached is not None and time.monotonic() < _refresh_cooldowns.get(data_file, 0)
        try:
            if cooling_down:
                raise VendorUnavailableError("Yahoo Finance refresh is cooling down after a recent failure")
            downloaded = yf_retry(lambda: yf.Ticker(canonical).history(
                start=start_str,
                end=end_str,
                auto_adjust=True,
                actions=False,
            ))
            if downloaded is None or downloaded.empty:
                raise_for_empty(symbol, canonical, "price rows")
            try:
                downloaded = _clean_dataframe(downloaded.reset_index())
            except (ValueError, KeyError) as exc:
                raise VendorUnavailableError("Yahoo Finance returned malformed daily price data") from exc
            usable = downloaded.loc[(downloaded["Date"] <= as_of_dt) & downloaded["Close"].notna()]
            if usable.empty:
                raise NoMarketDataError(symbol, canonical, "no bar in range has a closing price")
            _assert_ohlcv_not_stale(usable, as_of_date, symbol, canonical)
            # Validate before replacing a good cache with a malformed response.
            replace_file(data_file, lambda temp: downloaded.to_csv(temp, index=False, encoding="utf-8"))
            data = downloaded
        except VendorUnavailableError as refresh_error:
            if cached is None:
                raise
            try:
                data = _fallback_rows(cached, data_file, as_of_dt, symbol, canonical, config, now_utc)
                if start_date is not None and data["Date"].min() > pd.Timestamp(start_date):
                    raise NoMarketDataError(symbol, canonical, "cache does not cover the requested start date")
            except (NoMarketDataError, ValueError):
                if not config.get("market_history_reference_fallback", False):
                    raise VendorUnavailableError(
                        f"{refresh_error}; no sufficiently complete, current cache is available for {canonical}"
                    ) from None
                try:
                    data = _historical_reference_rows(cached, data_file, as_of_dt, symbol, canonical, config, now_utc)
                    if start_date is not None and data["Date"].min() > pd.Timestamp(start_date):
                        raise NoMarketDataError(symbol, canonical, "historical cache does not cover requested start")
                except (NoMarketDataError, ValueError):
                    raise VendorUnavailableError(
                        f"{refresh_error}; no sufficiently complete, current cache or usable historical reference is available for {canonical}"
                    ) from None
                historical_reference = True
            source = ("Yahoo Finance historical-reference cache" if historical_reference else "Yahoo Finance validated cache fallback")
            refresh_failed = True
            if not cooling_down:
                if len(_refresh_cooldowns) >= 256:
                    _refresh_cooldowns.pop(next(iter(_refresh_cooldowns)))
                _refresh_cooldowns[data_file] = time.monotonic() + OHLCV_REFRESH_COOLDOWN_SECONDS
            logger.warning("%s: live refresh failed; using completed cached session %s", canonical, data["Date"].max().date())

    data = _clean_dataframe(data)

    # Filter to as_of_date to prevent look-ahead bias in backtesting.
    data = data[data["Date"] <= as_of_dt]

    # A closeless newest bar is an unsettled session, not a symbol without data.
    # _fill_price_gaps below drops it, here and mid-series alike, so the frame
    # ends at the last settled bar; only a range with no close anywhere is no
    # data (#1201, #1289).
    if not data.empty and pd.isna(data["Close"].iloc[-1]):
        settled = data["Close"].notna().to_numpy().nonzero()[0]
        if settled.size == 0:
            raise NoMarketDataError(
                symbol, canonical, "no bar in range has a closing price"
            )
        logger.warning(
            "%s: %d trailing bar(s) through %s have no closing price; using %s "
            "as the latest close.", canonical, len(data) - settled[-1] - 1,
            data["Date"].iloc[-1].date(), data["Date"].iloc[settled[-1]].date(),
        )

    # Indicators need a continuous series, so gaps are carried forward. A caller
    # that reports the numbers themselves asks for the frame as it was reported:
    # a filled cell is the previous session's price under this session's date.
    data = _fill_price_gaps(data) if fill_gaps else data.dropna(subset=["Close"]).copy()

    # Reject a stale frame (latest row far older than as_of_date) rather than
    # feeding year-old prices into indicators (#1021).
    _assert_ohlcv_not_stale(data, as_of_date, symbol, canonical)

    if data.empty:
        raise NoMarketDataError(symbol, canonical, "no bar in range has a closing price")
    expected = latest_completed_session(as_of_dt, now_utc) if is_us_equity(canonical, config) else None
    fetched = pd.Timestamp.fromtimestamp(os.path.getmtime(data_file), tz="UTC")
    data.attrs["market_data"] = {
        "source": source,
        "fetched_at": fetched.isoformat(),
        "latest_row": data["Date"].iloc[-1].strftime("%Y-%m-%d"),
        "last_completed_session": expected.strftime("%Y-%m-%d") if expected is not None else None,
        "provisional": bool(expected is not None and data["Date"].iloc[-1] > expected),
        "historical": as_of_dt.date() < now_utc.tz_convert("America/New_York").date(),
        "missing_completed_session": bool(expected is not None and data["Date"].iloc[-1] < expected),
        "refresh_failed": refresh_failed,
        "historical_reference": historical_reference,
    }

    return data
