"""Conservative daily-bar boundaries for US listed equities, without an API.

Rules follow https://www.nyse.com/trade/hours-calendars (checked 2026-11-09).
These are scheduled NYSE/Nasdaq holidays, not an authoritative exchange calendar:
unexpected closures and exchange-specific instruments need separate validation.
The clock is always converted to New York, including DST. A 30 minute publication
buffer avoids calling a bar final at the closing bell.
"""
from __future__ import annotations

import re
from datetime import timedelta

import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar, GoodFriday, Holiday, USLaborDay,
    USMartinLutherKingJr, USMemorialDay, USPresidentsDay, USThanksgivingDay,
    nearest_workday, sunday_to_monday,
)

NY_TZ = "America/New_York"
PUBLICATION_BUFFER = pd.Timedelta(minutes=30)


class USEquityHolidays(AbstractHolidayCalendar):
    rules = [
        # Unlike most federal holidays, a Saturday New Year does NOT close
        # the preceding Friday (e.g. NYSE was open 2021-12-31).
        Holiday("New Year", month=1, day=1, observance=sunday_to_monday),
        USMartinLutherKingJr, USPresidentsDay, GoodFriday, USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, start_date="2022-01-01", observance=nearest_workday),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay, USThanksgivingDay,
        Holiday("Christmas", month=12, day=25, observance=nearest_workday),
    ]


# Known full-day extraordinary closures within/near common historical ranges.
# This list is explicit; the output still states that the calendar is rule based.
EXTRA_CLOSURES = frozenset(pd.Timestamp(day) for day in (
    "2001-09-11", "2001-09-12", "2001-09-13", "2001-09-14",
    "2004-06-11", "2007-01-02", "2012-10-29", "2012-10-30",
    "2018-12-05", "2025-01-09",
))


def is_us_equity(symbol: str, config: dict | None = None) -> bool:
    """Yahoo plain tickers are US listed; suffix/FX/futures/crypto stay unknown.

    An explicit override supports aliases and exceptions. This does not assign
    US holidays to e.g. 7203.T, GC=F, EURUSD=X or BTC-USD.
    """
    override = (config or {}).get("yahoo_market_calendars", {}).get(symbol)
    if override is not None:
        return override == "US_EQUITY"
    return bool(re.fullmatch(r"[A-Z][A-Z0-9]*(?:-[A-Z])?", symbol))


def is_session(day: pd.Timestamp) -> bool:
    day = pd.Timestamp(day).normalize().tz_localize(None)
    if day.weekday() >= 5 or day in EXTRA_CLOSURES:
        return False
    return day not in USEquityHolidays().holidays(day, day)


def session_close(day: pd.Timestamp) -> pd.Timestamp:
    """Scheduled close in New York; caller must first verify this is a session."""
    day = pd.Timestamp(day).normalize().tz_localize(None)
    thanksgiving = USThanksgivingDay.dates(f"{day.year}-01-01", f"{day.year}-12-31")[0]
    early = (
        day == thanksgiving + pd.Timedelta(days=1)
        or (day.month == 7 and day.day == 3)
        or (day.month == 12 and day.day == 24)
    )
    return (day + pd.Timedelta(hours=13 if early else 16)).tz_localize(NY_TZ)


def latest_completed_session(as_of: pd.Timestamp, now_utc: pd.Timestamp) -> pd.Timestamp:
    """Last scheduled session published by now and no later than requested date."""
    now_ny = now_utc.tz_convert(NY_TZ)
    day = min(pd.Timestamp(as_of).normalize().tz_localize(None), now_ny.tz_localize(None).normalize())
    # Finite search: a missing calendar boundary should fail, not hang a tool.
    for _ in range(20):
        if is_session(day) and now_ny >= session_close(day) + PUBLICATION_BUFFER:
            return day
        day -= timedelta(days=1)
    raise ValueError("No scheduled US equity session found in the previous 20 days")
