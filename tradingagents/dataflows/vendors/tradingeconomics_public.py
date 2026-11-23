"""Read only the public calendar's explicitly labelled Consensus cells.

This is a partial, current-page supplement, not the licensed API or a historical
vintage. Reject schema/timezone drift. No positional column reads, fuzzy event
matches, proprietary Forecast fallback, credentials or challenge bypasses.
"""
from datetime import UTC, datetime
from html.parser import HTMLParser
import re

import requests

PUBLIC_URL = "https://tradingeconomics.com/united-states/calendar"
PROVIDER = "trading_economics_public"
USER_AGENT = "TradingAgents/0.7.1 (economic-calendar research)"

# Yahoo exact labels -> public calendar data-event, measurement unit.
# All are distinct series; in particular monthly/yearly/core/index never merge.
EVENTS = {
    "CPI MM, SA": ("inflation rate mom", "%"),
    "Core CPI MM, SA": ("core inflation rate mom", "%"),
    "CPI YY, NSA": ("inflation rate yoy", "%"),
    "Core CPI YY, NSA": ("core inflation rate yoy", "%"),
    "CPI Index, NSA": ("cpi", "unspecified"),
    "PPI Final Demand MM": ("ppi mom", "%"),
    "PPI Final Demand YY": ("ppi yoy", "%"),
    "PPI exFood/Energy MM": ("core ppi mom", "%"),
    "PPI exFood/Energy YY": ("core ppi yoy", "%"),
    "PPI ex Food/Energy/Tr MM": ("ppi ex food, energy and trade mom", "%"),
    "PPI ex Food/Energy/Tr YY": ("ppi ex food, energy and trade yoy", "%"),
    "Retail Sales MM": ("retail sales mom", "%"),
    "Retail Control": ("retail sales control group mom", "%"),
    "Retail Sales Ex-Autos MM": ("retail sales ex autos mom", "%"),
    "Retail Ex Gas/Autos": ("retail sales ex gas/autos mom", "%"),
    "Unemployment Rate": ("unemployment rate", "%"),
}
MONTHS = frozenset("jan feb mar apr may jun jul aug sep oct nov dec".split())


def event_spec(name):
    return EVENTS.get(name.rstrip("* "))


def _clean(value):
    return " ".join(value.split())


def _month(value):
    value = _clean(value).lower()
    return value if value in MONTHS else None


class _Calendar(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.row, self.depth, self.cell = None, 0, None
        self.capture, self.capture_tag = None, None
        self.timezone, self.offsets, self.rows = False, [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "select" and a.get("id") == "DropDownListTimezone":
            self.timezone = True
        if tag == "option" and self.timezone and "selected" in a:
            self.offsets.append(a.get("value"))
        if tag == "tr":
            if self.row is not None:
                self.depth += 1  # nested country table must not truncate the outer row
            elif a.get("data-country", "").strip().lower() == "united states" and a.get("data-id"):
                self.row = {"attrs": a, "cells": [], "fields": {}, "counts": {}}
                self.depth = 1
        if self.row is None:
            return
        if tag == "td" and self.depth == 1:
            self.row["cells"].append({"attrs": a, "text": []})
            self.cell = len(self.row["cells"]) - 1
        field = "consensus" if a.get("id") == "consensus" else (
            "period" if "calendar-reference" in a.get("class", "").split() else None)
        if field:
            self.row["counts"][field] = self.row["counts"].get(field, 0) + 1
            self.row["fields"].setdefault(field, [])
            self.capture, self.capture_tag = field, tag

    def handle_data(self, data):
        if self.row is not None:
            if self.cell is not None:
                self.row["cells"][self.cell]["text"].append(data)
            if self.capture is not None:
                self.row["fields"][self.capture].append(data)

    def handle_endtag(self, tag):
        if tag == "select":
            self.timezone = False
        if tag == self.capture_tag:
            self.capture = self.capture_tag = None
        if self.row is not None:
            if tag == "td" and self.depth == 1:
                self.cell = None
            if tag == "tr":
                self.depth -= 1
                if self.depth == 0:
                    self.rows.append(self.row)
                    self.row, self.cell = None, None


def parse_calendar(html):
    for label in ("Actual", "Previous", "Consensus", "Forecast"):
        if not re.search(r"<th\b[^>]*>\s*" + label + r"\s*</th>", html, re.I):
            raise ValueError("Public calendar headers changed")
    parser = _Calendar()
    parser.feed(html)
    parser.close()
    if not parser.offsets or set(parser.offsets) != {"0"}:
        raise ValueError("Public calendar timezone must explicitly be UTC")
    if not parser.rows:
        raise ValueError("Public calendar contains no recognized US rows")
    records = []
    for row in parser.rows:
        if not row["cells"] or row["counts"].get("consensus") != 1 or row["counts"].get("period") != 1:
            continue
        first = row["cells"][0]
        day = re.search(r"\b\d{4}-\d{2}-\d{2}\b", first["attrs"].get("class", ""))
        clock = _clean("".join(first["text"]))
        if not day or not re.fullmatch(r"\d{1,2}:\d{2} [AP]M", clock):
            continue
        try:
            release = datetime.strptime(day[0] + " " + clock, "%Y-%m-%d %I:%M %p").replace(tzinfo=UTC)
        except ValueError:
            continue
        period = _month("".join(row["fields"]["period"]))
        a = row["attrs"]
        path = a.get("data-url", "")
        if not period or not re.fullmatch(r"/united-states/[a-z0-9-]+", path):
            continue
        records.append({"event": a.get("data-event", "").strip().lower(),
                        "period": period, "release_at": release.isoformat(),
                        "consensus": _clean("".join(row["fields"]["consensus"])),
                        "row_id": a["data-id"], "url": "https://tradingeconomics.com" + path})
    return records


def fetch_calendar():
    response = requests.get(PUBLIC_URL, timeout=(6, 15), allow_redirects=False,
                            headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    if response.status_code != 200 or "text/html" not in response.headers.get("Content-Type", "").lower():
        raise ValueError("Public calendar did not return an HTML page")
    if len(response.content) > 5_000_000:
        raise ValueError("Public calendar page exceeded bounded size")
    return parse_calendar(response.text)


def supplement(events):
    """Fill missing expectations only on an exact series + month + UTC instant.

Keep Yahoo's event identity and actuals. The separate source metadata travels
with every filled expectation into the immutable local capture.
"""
    from tradingagents.dataflows.macro_events import quantity, _timestamp, _now

    candidates = [e for e in events if e["expected"] is None and event_spec(e["name"]) and _month(e["period"])]
    if not candidates:
        return events, []
    records = fetch_calendar()
    observed = _now().isoformat()
    filled, ambiguous = 0, 0
    for event in candidates:
        name, unit = event_spec(event["name"])
        matches = [r for r in records if r["event"] == name and r["period"] == _month(event["period"])
                   and _timestamp(r["release_at"]) == _timestamp(event["release_at"])]
        # Even equal-valued duplicate rows have unclear identities: do not choose.
        if len(matches) != 1:
            ambiguous += int(len(matches) > 1)
            continue
        row = matches[0]
        value = quantity(row["consensus"])
        if value is None or value["unit"] != unit:
            continue
        event["expected"] = value
        event["expected_source"] = {"provider": PROVIDER, "url": row["url"],
                                    "calendar_url": PUBLIC_URL, "row_id": row["row_id"],
                                    "release_at": row["release_at"], "period": row["period"],
                                    "observed_at": observed, "field": "Consensus"}
        filled += 1
    notes = [f"Public Consensus supplement filled {filled}/{len(candidates)} eligible missing expectations; "
             "exact series, reference month and UTC timestamp matches only. Current-page coverage is partial; Forecast was never used."]
    if ambiguous:
        notes.append(f"Withheld {ambiguous} ambiguous public-calendar matches.")
    return events, notes
