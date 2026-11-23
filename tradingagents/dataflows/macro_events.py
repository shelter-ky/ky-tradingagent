"""Dated US macro event snapshots, honest surprise arithmetic and local vintages.

FRED levels are separate from release events. Neither today's calendar nor its
forecast is an as-published historical vintage. A local pre-release capture can
establish when an estimate was observed, but not that a later actual is the
first release. No value in this module is a directional trading signal.
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from numbers import Real
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import get_current_date

VERSION = "macro-events-v1"
YAHOO_SOURCE = "https://finance.yahoo.com/calendar/economic"
TE_SOURCE = "https://docs.tradingeconomics.com/economic_calendar/schema/"


def _now():
    return datetime.now(UTC)


def _timestamp(value, *, vendor_utc=False):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            if not vendor_utc:
                return None
            result = result.replace(tzinfo=UTC)
        return result.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _text(value):
    if value is None or str(value).strip().lower() in {"", "nan", "none", "nat", "null"}:
        return ""
    return str(value).strip()


def quantity(value, unit=""):
    """Canonical base units for K/M/B and percentages; no percentage-ratio surprise.

Ranges, arbitrary text, nonfinite values and conflicting suffixes are missing.
Unspecified vendor units remain unspecified, never guessed to be people or bp.
"""
    if isinstance(value, bool):
        return None
    text, unit = _text(value), _text(unit)
    if isinstance(value, Real) and math.isfinite(value):
        text = format(Decimal(str(value)), "f")
    scale_match = re.search(r"\b(k|thousands?|m|mln|millions?|b|bn|billions?)\b", unit, re.I)
    unit_scale, base_unit = 1, unit
    if scale_match:
        term = scale_match[0].lower()
        unit_scale = 1000 if term.startswith(("k", "thousand")) else (
            1000000 if term.startswith(("m", "million")) else 1000000000)
        base_unit = (unit[:scale_match.start()] + unit[scale_match.end():]).strip() or "count"
    match = re.fullmatch(r"([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|[+-]?\.\d+)\s*([KMB%]?)", text, re.I)
    if not match:
        return None
    suffix = match[2].upper()
    if suffix == "%" and unit not in {"", "%"}:
        return None
    if unit == "%" and suffix in {"K", "M", "B"}:
        return None
    try:
        number = Decimal(match[1].replace(",", "")) * {"K": 1000, "M": 1000000, "B": 1000000000}.get(suffix, unit_scale)
        number = float(number)
    except (InvalidOperation, OverflowError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return {"value": number, "unit": "%" if suffix == "%" else
            base_unit or ("count" if suffix else "unspecified")}


def difference(actual, expected):
    if not actual or not expected or actual["unit"] != expected["unit"]:
        return None
    delta = float(Decimal(str(actual["value"])) - Decimal(str(expected["value"])))
    if not math.isfinite(delta):
        return None
    return {"value": delta, "unit": "pp" if actual["unit"] == "%" else actual["unit"]}


def _yahoo_unit(name):
    # Narrow label interpretation only. CPI index levels must not become rates.
    if re.fullmatch(r"(?:Core )?CPI (?:MM, SA|YY, NSA)", name.rstrip("* ")):
        return "%"
    if name.rstrip("* ") == "Unemployment Rate":
        return "%"
    from tradingagents.dataflows.vendors.tradingeconomics_public import event_spec
    spec = event_spec(name)
    if spec and spec[1] == "%":
        return "%"
    return ""


def _event(name, release, period, provider, *, actual=None, expected=None,
           previous=None, original_previous=None, unit="", precise=True):
    at = _timestamp(release, vendor_utc=provider == "trading_economics")
    if not at or not _text(name):
        return None
    return {"name": _text(name), "release_at": at.isoformat(), "period": _text(period),
            "provider": provider, "precise": precise,
            "actual": quantity(actual, unit), "expected": quantity(expected, unit),
            "previous": quantity(previous, unit), "original_previous": quantity(original_previous, unit)}


def _fetch(start, end, provider):
    if provider == "yahoo":
        from tradingagents.dataflows.vendors.yahoo.economic_calendar import fetch_us_calendar
        raw, warnings = fetch_us_calendar(start.isoformat(), (end + timedelta(days=1)).isoformat())
        events = [_event(r.get("Event"), r.get("Event Time"), r.get("For"), provider,
                         actual=r.get("Actual"), expected=r.get("Expected"), previous=r.get("Last"),
                         original_previous=r.get("Revised"), unit=_yahoo_unit(_text(r.get("Event"))))
                  for r in raw if r.get("Region") == "US"]
    elif provider == "trading_economics":
        key = os.environ.get("TRADING_ECONOMICS_API_KEY", "").strip()
        if not key:
            raise ValueError("TRADING_ECONOMICS_API_KEY is missing")
        # Forecast is survey consensus; TEForecast is proprietary, never a substitute.
        response = requests.get(
            f"https://api.tradingeconomics.com/calendar/country/united%20states/{start}/{end}",
            params={"c": key, "f": "json"}, timeout=20)
        response.raise_for_status()
        raw, warnings = response.json(), []
        if not isinstance(raw, list) or not all(isinstance(r, dict) for r in raw):
            raise ValueError("Trading Economics calendar schema changed")
        events = [_event(r.get("Event"), r.get("Date"), r.get("Reference"), provider,
                         actual=r.get("Actual"), expected=r.get("Forecast"), previous=r.get("Previous"),
                         original_previous=r.get("Revised"), unit=r.get("Unit", ""),
                         precise=str(r.get("DateSpan")) == "0")
                  for r in raw if r.get("Country") == "United States"]
    else:
        raise ValueError("Unknown macro_events_vendor")
    if any(e is None for e in events):
        warnings.append("Rows with missing/invalid release timestamps were withheld.")
    events = [e for e in events if e and start <= _timestamp(e["release_at"]).date() <= end]
    # Conflicting duplicate records are withheld, not resolved by feed order.
    unique, conflicts = {}, set()
    for event in events:
        key = _key(event)
        if key in unique and unique[key] != event:
            conflicts.add(key)
        unique[key] = event
    if conflicts:
        warnings.append("Conflicting duplicate calendar rows were withheld.")
    return [e for key, e in unique.items() if key not in conflicts], warnings


def _key(event):
    return (event["provider"], event["name"], event["period"], event["release_at"])


def _valid_event(event):
    if not isinstance(event, dict) or not all(isinstance(event.get(k), str) for k in
            ("name", "provider", "period", "release_at")) or not _timestamp(event["release_at"]):
        return False
    if not isinstance(event.get("precise"), bool):
        return False
    if not {"actual", "expected", "previous", "original_previous"}.issubset(event):
        return False
    source = event.get("expected_source")
    if source is not None and (not isinstance(source, dict)
                              or not all(isinstance(source.get(k), str) for k in ("provider", "url", "field", "observed_at"))
                              or not _timestamp(source["observed_at"])
                              or source.get("field") != "Consensus"
                              or _timestamp(source.get("release_at")) != _timestamp(event["release_at"])):
        return False
    for key in ("actual", "expected", "previous", "original_previous"):
        q = event.get(key)
        if q is not None and (not isinstance(q, dict) or not isinstance(q.get("unit"), str)
                             or isinstance(q.get("value"), bool)
                             or not isinstance(q.get("value"), (int, float)) or not math.isfinite(q["value"])):
            return False
    return True


def _read_archives(directory, cutoff, warnings):
    archives = []
    try:
        paths = sorted(directory.glob("*.json"), reverse=True)
        if len(paths) > 1000:
            warnings.append("Local archive scan limited to latest 1000 captures; historical coverage may be partial.")
        for path in paths[:1000]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                at = _timestamp(payload.get("captured_at"))
                if (payload.get("version") != VERSION or at is None or at > cutoff
                        or not isinstance(payload.get("events"), list)
                        or not all(_valid_event(e) for e in payload["events"])
                        or any(_timestamp(e["expected_source"]["observed_at"]) > at
                               for e in payload["events"] if e.get("expected_source"))):
                    continue
                archives.append(payload)
            except (OSError, ValueError, TypeError, AttributeError):
                warnings.append("An unreadable local macro capture was skipped.")
    except OSError:
        warnings.append("Local macro archive is unavailable.")
    return sorted(archives, key=lambda p: _timestamp(p["captured_at"]), reverse=True)


def _store(directory, events, at, warnings):
    # Immutable per-capture files avoid lost updates between simultaneous runs.
    path = directory / f"{at:%Y%m%dT%H%M%S%fZ}_{uuid4().hex}.json"
    temporary = path.with_suffix(".tmp")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        payload = {"version": VERSION, "captured_at": at.isoformat(), "events": events}
        temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    except (OSError, ValueError):
        warnings.append("Could not save this capture; it cannot establish a future pre-release estimate.")
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _display(q, signed=False):
    if not q:
        return "missing"
    value = f"{q['value']:+.8g}" if signed else f"{q['value']:.8g}"
    return f"{value} {q['unit']}"


def _safe(text):
    return str(text).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _format(events, archives, captured_at, cutoff, start, end, provider, warnings, historical):
    lines = ["## US macro release evidence", f"Analysis cutoff: {cutoff.isoformat()} (UTC). "
             f"Window: {start}..{end}. Source: {provider}; local capture: {captured_at.isoformat()}.",
             "Times below are vendor-reported UTC and America/New_York (DST-aware), not independently "
             "confirmed by the releasing agency. Coverage is partial; a missing CPI/PCE/FOMC event "
             "does not prove there is no event. Missing consensus is never replaced by FRED, a prior "
             "value or a proprietary forecast. Current actuals may include later revisions.",
             "Yahoo exact mapped CPI/PPI/retail and unemployment rate labels supply rate units (%); CPI Index is "
             "a level. Other unspecified units stay unspecified. Differences of percentage values "
             "are percentage points (pp), not relative percent changes or automatic buy/sell signals."]
    if historical:
        lines.append("Historical mode: only locally captured data observed by the cutoff; no live calendar was queried.")
    if any(e.get("archive_reference") for e in events):
        lines.insert(1, "ARCHIVED CALENDAR REFERENCE ONLY: live refresh failed. Original observation times are retained; schedules and estimates may have changed. New actuals and surprise gaps are unavailable, not zero. This is not a current calendar confirmation.")
    focus = re.compile(r"CPI|PCE|PPI|Payroll|Unemployment|Jobless|FOMC|Fed.*Rate|Interest Rate|GDP|ISM|PMI|Retail|U Mich|Inflation|Personal Consump", re.I)
    selected = [e for e in events if focus.search(e["name"])]
    selected.sort(key=lambda e: (0 if re.search(r"CPI|PCE|Payroll|Unemployment|FOMC|Fed.*Rate|Interest Rate", e["name"], re.I) else 1, e["release_at"], e["name"]))
    if len(selected) > 60:
        warnings.append("Focused event table capped at 60 rows; additional events retained in local capture.")
    selected = selected[:60]
    lines.append(f"Showing {len(selected)} focused inflation, employment, policy, growth and sentiment rows "
                 f"from {len(events)} returned US events. Other events remain in the local capture; "
                 f"vendor expected missing in {sum(e['expected'] is None for e in selected)}/{len(selected)} displayed rows.")
    available = [e for e in selected if e["expected"] is not None]
    available.sort(key=lambda e: (0 if "CPI" in e["name"] else 1, e["release_at"], e["name"]))
    if available:
        lines.append("### Available vendor expectations\n\n" + "\n".join(
            f"- {_safe(e['name'])}, {_safe(e['period'])}, {e['release_at']}: {_display(e['expected'])}; "
            f"source={(e.get('expected_source') or {}).get('provider', e['provider'])}."
            for e in available[:12]))
    table = ["| Event / period | Release UTC / New York | Actual snapshot | Vendor expected | Pre-release estimate (captured UTC) | Snapshot gap | Prior latest / originally reported / revision delta | Status | Consensus sources (snapshot / pre-release) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for event in sorted(selected, key=lambda e: (e["release_at"], e["name"])):
        event_capture = _timestamp(event.get("observed_at")) or captured_at
        release = _timestamp(event["release_at"])
        prerelease, observed = None, None
        pre_source = "missing"
        if event_capture < release and event["precise"] and event["expected"] is not None:
            prerelease, observed = event["expected"], event_capture
            pre_source = (event.get("expected_source") or {}).get("provider", event["provider"])
        for capture in archives:
            if observed is not None:
                break
            at = _timestamp(capture["captured_at"])
            if at >= min(release, event_capture) or at > cutoff:
                continue
            found = next((e for e in capture["events"] if _key(e) == _key(event)
                          and e["precise"] and e["actual"] is None and e["expected"] is not None), None)
            if found:
                prerelease, observed = found["expected"], at
                pre_source = (found.get("expected_source") or {}).get("provider", found["provider"])
                break
        actual = event["actual"] if release <= min(cutoff, event_capture) and event["precise"] else None
        if not event["precise"]:
            status = "estimated timing; actual/gap withheld"
        elif release > min(cutoff, event_capture):
            status = "scheduled; actual/gap withheld"
            if event.get("archive_reference") and release <= cutoff:
                status = "archive predates release; new actual/gap unavailable"
        elif actual is None:
            status = "release time passed; actual unavailable (not zero)"
        elif prerelease is not None:
            status = "actual snapshot vs archived pre-release estimate; not first-release surprise"
        else:
            status = "vendor snapshot gap only; pre-release consensus unverified"
        if historical:
            status += f"; row captured {event_capture.isoformat()}"
        elif event.get("archive_reference"):
            status += f"; archived row captured {event_capture.isoformat()}"
        expected = prerelease if prerelease is not None else event["expected"]
        gap = difference(actual, expected) if actual is not None else None
        revision = difference(event["previous"], event["original_previous"]) if actual is not None else None
        prior = f"{_display(event['previous'])} / {_display(event['original_previous']) if actual is not None else 'withheld'} / {_display(revision, True)}"
        times = f"{release:%Y-%m-%d %H:%M}Z / {release.astimezone(ZoneInfo('America/New_York')):%Y-%m-%d %H:%M %Z}"
        pre = f"{_display(prerelease)} ({observed.isoformat()})" if observed else "missing"
        source = event.get("expected_source") or {}
        origin = source.get("provider", event["provider"]) if event["expected"] is not None else "missing"
        if source.get("observed_at"):
            origin += f" @ {source['observed_at']}"
        table.append("| " + " | ".join(_safe(x) for x in (
            f"{event['name']} / {event['period'] or 'period unavailable'}", times, _display(actual),
            _display(event["expected"]), pre, _display(gap, True), prior, status, f"{origin} / {pre_source}")) + " |")
    lines.append("\n".join(table) if selected else "Calendar evidence unavailable/empty for this focus/window; do not infer no macro catalysts.")
    if warnings:
        lines.append("Coverage / data quality: " + " ".join(dict.fromkeys(warnings)))
    source = YAHOO_SOURCE if provider == "yahoo" else TE_SOURCE
    selected_keys = {_key(x) for x in selected}
    if any(e.get("expected_source") for e in events) or any(
            e.get("expected_source") for capture in archives for e in capture["events"] if _key(e) in selected_keys):
        lines.append("Consensus supplement: https://tradingeconomics.com/united-states/calendar — only the public "
                     "Consensus cell is used, never Forecast. Row identity/source/observation time are retained "
                     "in local captures. This is not a historical-vintage feed or independently verified final consensus.")
    lines.append(f"Source documentation/calendar: {source}\n"
                 "Interpret inflation, growth, employment and revisions jointly with yields and price reaction; "
                 "no standardized surprise score or market-direction claim is supported by this packet.")
    return "\n\n".join(lines)


def build_macro_event_context(trade_date):
    """Fetch once at the news node; later tool turns reuse its graph-state packet."""
    cfg, warnings = get_config(), []
    provider = cfg.get("macro_events_vendor", "yahoo")
    if not cfg.get("macro_events_enabled", True):
        return "## US macro release evidence\nMacro event retrieval disabled in run settings; consensus/surprise unavailable."
    now, today = _now(), date.fromisoformat(get_current_date())
    run_day = date.fromisoformat(str(trade_date))
    if run_day > today:
        return "## US macro release evidence\nFuture analysis date: current snapshots cannot establish future actuals/consensus."
    cutoff = now if run_day == today else min(now, datetime.combine(run_day, time.max, UTC))
    start, end = run_day - timedelta(days=7), run_day + timedelta(days=14)
    directory = Path(cfg["data_cache_dir"]) / VERSION
    archives = _read_archives(directory, cutoff, warnings)
    historical = run_day < today
    if historical:
        # Per-event newest capture as of cutoff; no present-day data enters a past run.
        latest = {}
        for capture in archives:
            for event in capture["events"]:
                if event["provider"] == provider and start <= _timestamp(event["release_at"]).date() <= end:
                    latest.setdefault(_key(event), dict(event, observed_at=capture["captured_at"]))
        captured_at = max((_timestamp(e["observed_at"]) for e in latest.values()), default=cutoff)
        return _format(list(latest.values()), archives, captured_at, cutoff, start, end, provider, warnings, True)
    if provider == "trading_economics" and not os.environ.get("TRADING_ECONOMICS_API_KEY", "").strip():
        warnings.append("Trading Economics requires TRADING_ECONOMICS_API_KEY and calendar API entitlement.")
    elif provider not in {"yahoo", "trading_economics"}:
        warnings.append("Unknown macro_events_vendor; choose yahoo or trading_economics.")
    try:
        events, notes = _fetch(start, end, provider)
        warnings.extend(notes)
        supplement_vendor = cfg.get("macro_consensus_supplement", "trading_economics_public")
        if provider == "yahoo" and supplement_vendor:
            try:
                if supplement_vendor != "trading_economics_public":
                    raise ValueError("Unknown consensus supplement")
                from tradingagents.dataflows.vendors.tradingeconomics_public import supplement
                events, notes = supplement(events)
                warnings.extend(notes)
            except Exception as exc:
                warnings.append(f"Public consensus supplement unavailable ({type(exc).__name__}); "
                                "Yahoo events retained and missing consensus not replaced by proprietary forecasts.")
        captured_at = _now()  # Timestamp when the response was actually observed, not before network I/O.
        for event in events:
            if _timestamp(event["release_at"]) > captured_at or not event["precise"]:
                event["actual"], event["original_previous"] = None, None
        _store(directory, events, captured_at, warnings)
    except Exception as exc:
        # Do not print request URLs/exceptions: providers may carry API credentials in them.
        warnings.append(f"Live calendar unavailable ({type(exc).__name__}); cached evidence, if present, is historical reference only.")
        latest = {}
        for capture in archives:
            at = _timestamp(capture["captured_at"])
            if now - at > timedelta(hours=24):
                continue
            for event in capture["events"]:
                if event["provider"] == provider and start <= _timestamp(event["release_at"]).date() <= end:
                    latest.setdefault(_key(event), dict(event, observed_at=capture["captured_at"],
                                                       actual=None, original_previous=None, archive_reference=True))
        events = list(latest.values())
        captured_at = max((_timestamp(e["observed_at"]) for e in events), default=now)
        # Never save a failed refresh as a new observation or a new pre-release capture.
    return _format(events, archives, captured_at, _now(), start, end, provider, warnings, False)
