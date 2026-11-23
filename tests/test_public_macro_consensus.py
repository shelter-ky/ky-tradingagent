"""Public Consensus cells, exact cross-source identity and pre-release provenance."""
import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tradingagents.dataflows import macro_events as macro
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.vendors import tradingeconomics_public as public


def row(*, consensus="0.3%", forecast="9.9%", event="inflation rate mom", period="OCT",
        day="2026-11-10", time="01:30 PM", country="united states", extra=""):
    return f'''<tr data-country="{country}" data-id="123" data-event="{event}" data-url="/united-states/inflation-rate-mom">
    <td class=" {day}"><span>{time}</span></td>
    <td><table><tr><td><div title="United States">US</div></td></tr></table></td>
    <td><a class="calendar-event">{event}</a><span class="calendar-reference">{period}</span></td>
    <td><span id="actual">7.7%</span></td><td><span id="previous">6.6%</span></td>
    <td><a id="consensus">{consensus}</a>{extra}</span></td>
    <td><a id="forecast">{forecast}</a></span></td></tr>'''


def html(rows=None, offset="0"):
    return f'''<select id="DropDownListTimezone"><option selected="selected" value="{offset}">UTC</option></select>
    <table><thead><tr><th>Actual</th><th>Previous</th><th>Consensus</th><th>Forecast</th></tr></thead>
    {row() if rows is None else rows}</table>'''


def event(**kwargs):
    values = dict(name="CPI MM, SA", release="2026-11-10T13:30:00Z", period="Oct", provider="yahoo", unit="%")
    values.update(kwargs)
    return macro._event(**values)


@pytest.fixture
def clock(monkeypatch):
    now = [datetime(2026, 11, 9, 15, tzinfo=UTC)]
    monkeypatch.setattr(macro, "_now", lambda: now[0])
    monkeypatch.setattr(macro, "get_current_date", lambda: now[0].date().isoformat())
    return now


def test_parser_reads_consensus_not_forecast_and_survives_nested_country_table():
    records = public.parse_calendar(html())
    assert len(records) == 1
    assert records[0]["consensus"] == "0.3%" and records[0]["release_at"] == "2026-11-10T13:30:00+00:00"
    assert records[0]["period"] == "oct"
    assert "forecast" not in records[0] and "actual" not in records[0]


@pytest.mark.parametrize("document", [html(offset="-300"), html().replace('selected="selected"', ''),
    html().replace("Consensus</th>", "Estimate</th>"), html(rows=row(country="china"))])
def test_unknown_timezone_headers_and_country_fail_closed(document):
    with pytest.raises(ValueError):
        public.parse_calendar(document)


@pytest.mark.parametrize("kwargs", [dict(time="Tentative"), dict(time="All Day"), dict(period=""),
    dict(day="2026-13-10"), dict(extra='<a id="consensus">0.4%</a>')])
def test_uncertain_or_ambiguous_row_fields_are_not_parsed(kwargs):
    assert public.parse_calendar(html(rows=row(**kwargs))) == []


def test_supplement_preserves_actuals_and_previous_values(monkeypatch, clock):
    monkeypatch.setattr(public, "fetch_calendar", lambda: public.parse_calendar(html()))
    e = event(actual=.1, previous=.6)
    events, notes = public.supplement([e])
    assert events[0]["expected"] == dict(value=.3, unit="%")
    assert events[0]["actual"] == dict(value=.1, unit="%") and events[0]["previous"] == dict(value=.6, unit="%")
    source = events[0]["expected_source"]
    assert source["field"] == "Consensus" and source["provider"] == public.PROVIDER
    assert source["observed_at"] == "2026-11-09T15:00:00+00:00"
    assert "1/1" in notes[0] and macro._valid_event(events[0])


@pytest.mark.parametrize("kwargs", [dict(period="Sep"), dict(release="2026-11-10T14:30:00Z"),
    dict(name="Core CPI MM, SA"), dict(name="CPI YY, NSA"), dict(name="CPI Index, NSA")])
def test_different_period_timestamp_core_yearly_and_index_never_merge(monkeypatch, clock, kwargs):
    monkeypatch.setattr(public, "fetch_calendar", lambda: public.parse_calendar(html()))
    e = event(**kwargs)
    assert public.supplement([e])[0][0]["expected"] is None


def test_duplicate_source_rows_are_withheld(monkeypatch, clock):
    monkeypatch.setattr(public, "fetch_calendar", lambda: public.parse_calendar(html(rows=row()+row())))
    events, notes = public.supplement([event()])
    assert events[0]["expected"] is None and "ambiguous" in " ".join(notes)


@pytest.mark.parametrize("value,expected", [("", None), ("0", None), ("0%", 0), ("unknown", None), (".3%", .3)])
def test_missing_consensus_never_uses_forecast_and_zero_is_valid(monkeypatch, clock, value, expected):
    monkeypatch.setattr(public, "fetch_calendar", lambda: public.parse_calendar(html(rows=row(consensus=value))))
    e = public.supplement([event()])[0][0]
    assert e["expected"] == (None if expected is None else dict(value=expected, unit="%"))


def test_existing_yahoo_expectation_and_unknown_period_do_not_trigger_fetch(monkeypatch, clock):
    monkeypatch.setattr(public, "fetch_calendar", lambda: pytest.fail("should not fetch"))
    e = event(expected=0)
    assert public.supplement([e])[0][0]["expected"] == dict(value=0, unit="%")
    assert public.supplement([event(period="")])[0][0]["expected"] is None


def test_supplement_outage_keeps_yahoo_events_and_does_not_claim_absence(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(macro, "_fetch", lambda *a: ([event()], []))
    monkeypatch.setattr(public, "fetch_calendar", lambda: (_ for _ in ()).throw(RuntimeError("outage")))
    with run_config({"data_cache_dir": str(tmp_path)}):
        packet = macro.build_macro_event_context("2026-11-09")
    assert "CPI MM, SA" in packet and "Yahoo events retained" in packet
    assert "Public consensus supplement unavailable" in packet


def test_live_capture_freezes_public_estimate_and_historical_replay_never_fetches(tmp_path, monkeypatch, clock):
    actual, consensus = [None], ["0.3%"]
    monkeypatch.setattr(macro, "_fetch", lambda *a: ([event(actual=actual[0])], []))
    monkeypatch.setattr(public, "fetch_calendar", lambda: public.parse_calendar(html(rows=row(consensus=consensus[0]))))
    with run_config({"data_cache_dir": str(tmp_path)}):
        before = macro.build_macro_event_context("2026-11-09")
        assert "Available vendor expectations" in before and "0.3 %" in before
        clock[0] = datetime(2026, 11, 10, 15, tzinfo=UTC)
        actual[0], consensus[0] = .4, "0.8%"
        after = macro.build_macro_event_context("2026-11-10")
        assert "| 0.4 % | 0.8 % | 0.3 % (2026-11-09T15:00:00+00:00) | +0.1 pp |" in after
        assert "not first-release surprise" in after and public.PROVIDER in after
        clock[0] = datetime(2026, 11, 11, 15, tzinfo=UTC)
        monkeypatch.setattr(public, "fetch_calendar", lambda: pytest.fail("historical live fetch"))
        history = macro.build_macro_event_context("2026-11-09")
        assert "0.8 %" not in history and "0.4 %" not in history
        assert "0.3 %" in history and "Historical mode" in history


def test_source_observation_after_archive_time_is_not_historical_evidence(tmp_path, clock):
    e = event(expected=.3)
    e["expected_source"] = dict(provider=public.PROVIDER, url=public.PUBLIC_URL, field="Consensus",
        observed_at="2026-11-10T15:00:00+00:00", release_at=e["release_at"])
    directory = tmp_path / macro.VERSION
    directory.mkdir()
    (directory / "bad-time.json").write_text(json.dumps(dict(version=macro.VERSION,
        captured_at="2026-11-09T15:00:00+00:00", events=[e])), encoding="utf-8")
    assert macro._read_archives(directory, clock[0], []) == []


def test_rate_unit_mapping_does_not_turn_indices_into_percentages():
    assert macro._yahoo_unit("PPI Final Demand MM") == "%"
    assert macro._yahoo_unit("Retail Sales MM") == "%"
    assert macro._yahoo_unit("CPI Index, NSA") == ""


def test_fetch_is_bounded_and_access_errors_are_not_bypassed(monkeypatch):
    calls = []
    document = html()
    monkeypatch.setattr(public.requests, "get", lambda url, **kw: calls.append((url, kw)) or SimpleNamespace(
        status_code=200, headers={"Content-Type": "text/html"}, content=document.encode(), text=document,
        raise_for_status=lambda: None))
    assert public.fetch_calendar()
    assert calls[0][0] == public.PUBLIC_URL and calls[0][1]["timeout"] == (6, 15)
    assert calls[0][1]["allow_redirects"] is False
