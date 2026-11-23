"""Small, schema-checked adapter for Yahoo's US economic calendar.

yfinance's normal cleanup replaces legitimate numeric zeroes with NaN. Keep
zeroes here and let the macro layer distinguish an absent cell from zero.
The two protected hooks isolate Yahoo's country filter and raw-column mapping;
a changed upstream schema fails explicitly instead of changing field meanings.
"""
import pandas as pd
import yfinance as yf
from yfinance.calendars import CalendarQuery

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo.common import yf_retry


class USEconomicCalendar(yf.Calendars):
    def _get_data(self, calendar_type, query, **kwargs):
        if calendar_type != "economic_event" or query.operator != "AND":
            raise ValueError("Unsupported Yahoo economic-calendar query schema")
        # Yahoo rejects nested AND expressions. Preserve both date bounds.
        query = CalendarQuery("and", [*query.operands, CalendarQuery("eq", ["country_code", "US"])])
        return super()._get_data(calendar_type, query, **kwargs)

    def _cleanup_df(self, calendar_type, df):
        if df.empty:
            return df
        required = {"Event", "Country Code", "Event Time", "For", "Actual",
                    "Market Expectation", "Prior to This", "Revised from"}
        if calendar_type != "economic_event" or not required.issubset(df.columns):
            raise ValueError("Yahoo economic-calendar columns changed")
        df = df.copy().set_index("Event").rename(columns={
            "Country Code": "Region", "Market Expectation": "Expected",
            "Prior to This": "Last", "Revised from": "Revised"})
        for column in ("Actual", "Expected", "Last", "Revised"):
            df[column] = pd.to_numeric(df[column], errors="coerce")
        df["Event Time"] = pd.to_datetime(df["Event Time"], utc=True, errors="coerce")
        return df


def fetch_us_calendar(start, end):
    """Return records and coverage warnings; bounded pagination, no silent tail loss."""
    calendar = USEconomicCalendar(start=start, end=end)
    rows, warnings, seen = [], [], set()
    for page in range(5):  # <=500 US rows for the bounded research window
        df = yf_retry(lambda: calendar.get_economic_events_calendar(
            limit=100, offset=page * 100), max_retries=1, base_delay=1)
        if not isinstance(df, pd.DataFrame):
            raise VendorUnavailableError("Yahoo economic calendar returned no table")
        if df.empty:
            break
        signature = df.to_json(orient="split", date_format="iso")
        if signature in seen:
            warnings.append("Yahoo repeated a calendar page; coverage is partial.")
            break
        seen.add(signature)
        rows.extend(dict(row, Event=name) for name, row in df.iterrows())
        if len(df) < 100:
            break
    else:
        warnings.append("Yahoo 500-row limit reached; coverage is partial.")
    return rows, warnings
