"""Release-time boundaries, archived expectations, revisions, units and failure paths."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd
import pytest
from langchain_core.messages import AIMessage

from tradingagents.dataflows import macro_events as macro
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.vendors.yahoo import economic_calendar as yahoo


def event(**kw):
    defaults = dict(name="CPI MM, SA", release="2026-11-10T13:30:00Z", period="Oct", provider="yahoo",
                    actual=None, expected=.2, previous=.4, unit="%")
    defaults.update(kw)
    return macro._event(**defaults)


@pytest.fixture
def clock(monkeypatch):
    value = [datetime(2026, 11, 9, 12, tzinfo=UTC)]
    monkeypatch.setattr(macro, "_now", lambda: value[0])
    monkeypatch.setattr(macro, "get_current_date", lambda: value[0].date().isoformat())
    return value


@pytest.mark.parametrize("value,unit,expected", [
    (0, "%", (0, "%")), (0.00001, "", (.00001, "unspecified")),
    ("178K", "", (178000, "count")), (".178M", "", (178000, "count")),
    ("1.2", "Millions", (1200000, "count")), ("178K", "Thousand", (178000, "count")),
    ("1.2B", "USD Billion", (1200000000, "USD")), ("1.2", "Billion USD", (1200000000, "USD")),
    ("178K", "Thousand Persons", (178000, "Persons")),
    ("-0.2%", "", (-.2, "%")), ("1,000", "", (1000, "unspecified")),
])
def test_quantity_canonical_units(value, unit, expected):
    assert macro.quantity(value, unit) == dict(value=expected[0], unit=expected[1])


@pytest.mark.parametrize("value,unit", [(None, ""), (float("nan"), ""), (float("inf"), ""),
    (True, ""), ("1-2", "%"), ("unknown", ""), ("1K", "%"), ("1%", "index"), ("12,34", "")])
def test_invalid_quantities_do_not_become_zero(value, unit):
    assert macro.quantity(value, unit) is None


def test_gaps_are_percentage_points_or_same_base_units():
    assert macro.difference(macro.quantity(".3%"), macro.quantity(".2%")) == dict(value=.1, unit="pp")
    assert macro.difference(macro.quantity("178K"), macro.quantity(".175M")) == dict(value=3000, unit="count")
    assert macro.difference(macro.quantity("0%"), macro.quantity("0%")) == dict(value=0, unit="pp")
    assert macro.difference(macro.quantity(".3%"), macro.quantity(".2")) is None
    assert macro.difference(None, macro.quantity("0%")) is None


def test_yahoo_preserves_zero_and_maps_revision_direction():
    raw = pd.DataFrame([{"Event": "CPI MM, SA", "Country Code": "US", "Event Time": "2026-11-10T13:30:00Z",
                         "For": "Oct", "Actual": "0.0", "Market Expectation": 0,
                         "Prior to This": ".3", "Revised from": ".4"}])
    df = yahoo.USEconomicCalendar()._cleanup_df("economic_event", raw)
    assert df.iloc[0]["Actual"] == df.iloc[0]["Expected"] == 0
    assert df.iloc[0]["Last"] == .3 and df.iloc[0]["Revised"] == .4
    with pytest.raises(ValueError, match="columns changed"):
        yahoo.USEconomicCalendar()._cleanup_df("economic_event", raw.drop(columns="Market Expectation"))


def test_yahoo_country_filter_keeps_date_bounds_flat(monkeypatch):
    from yfinance.calendars import CalendarQuery, Calendars
    seen = []
    monkeypatch.setattr(Calendars, "_get_data", lambda self, kind, query, **kw: seen.append(query.to_dict()))
    q = CalendarQuery("and", [CalendarQuery("gte", ["startdatetime", "2026-11-02"]),
                              CalendarQuery("lt", ["startdatetime", "2026-11-24"])])
    yahoo.USEconomicCalendar()._get_data("economic_event", q)
    assert len(q.operands) == 2
    assert len(seen[0]["operands"]) == 3
    assert seen[0]["operands"][-1] == {"operator": "EQ", "operands": ["country_code", "US"]}


def test_pagination_and_repeated_page_coverage(monkeypatch):
    df = pd.DataFrame({"Region": ["US"] * 100}, index=[f"CPI {i}" for i in range(100)])
    offsets = []
    monkeypatch.setattr(yahoo.USEconomicCalendar, "get_economic_events_calendar",
                        lambda self, **kw: offsets.append(kw["offset"]) or df)
    rows, warnings = yahoo.fetch_us_calendar("2026-11-02", "2026-11-24")
    assert len(rows) == 100 and offsets == [0, 100]
    assert "coverage is partial" in " ".join(warnings)


def test_missing_consensus_and_zero_actual_are_distinct(tmp_path, monkeypatch, clock):
    clock[0] = datetime(2026, 11, 10, 14, tzinfo=UTC)
    monkeypatch.setattr(macro, "_fetch", lambda *a: ([event(actual=0, expected=None)], []))
    with run_config({"data_cache_dir": str(tmp_path)}):
        text = macro.build_macro_event_context("2026-11-10")
    assert "| 0 % | missing | missing | missing |" in text
    assert "vendor expected missing in 1/1" in text
    assert "pre-release consensus unverified" in text


def test_pre_release_capture_survives_later_consensus_changes(tmp_path, monkeypatch, clock):
    values = [event(actual=99, expected=.2, original_previous=99)]  # future actual/revision must be scrubbed
    monkeypatch.setattr(macro, "_fetch", lambda *a: (values, []))
    with run_config({"data_cache_dir": str(tmp_path)}):
        before = macro.build_macro_event_context("2026-11-09")
        assert "99" not in before and "scheduled; actual/gap withheld" in before
        capture = json.loads(next((tmp_path / macro.VERSION).glob("*.json")).read_text())
        assert capture["events"][0]["actual"] is None
        assert capture["events"][0]["original_previous"] is None
        assert "0.2 % (2026-11-09T12:00:00+00:00)" in before
        clock[0] = datetime(2026, 11, 10, 14, tzinfo=UTC)
        values[:] = [event(actual=.3, expected=.8, previous=.3, original_previous=.4)]
        after = macro.build_macro_event_context("2026-11-10")
    assert "| 0.3 % | 0.8 % | 0.2 % (2026-11-09T12:00:00+00:00) | +0.1 pp |" in after
    assert "0.3 % / 0.4 % / -0.1 pp" in after
    assert "not first-release surprise" in after
    assert len(list((tmp_path / macro.VERSION).glob("*.json"))) == 2


def test_post_release_snapshot_is_not_a_frozen_consensus(tmp_path, monkeypatch, clock):
    clock[0] = datetime(2026, 11, 10, 14, tzinfo=UTC)
    monkeypatch.setattr(macro, "_fetch", lambda *a: ([event(actual=.3, expected=.2)], []))
    with run_config({"data_cache_dir": str(tmp_path)}):
        text = macro.build_macro_event_context("2026-11-10")
        clock[0] = datetime(2026, 11, 10, 15, tzinfo=UTC)
        repeated = macro.build_macro_event_context("2026-11-10")
    for packet in (text, repeated):
        assert "| 0.3 % | 0.2 % | missing | +0.1 pp |" in packet
        assert "pre-release consensus unverified" in packet


def test_historical_replay_uses_only_local_vintages(tmp_path, monkeypatch, clock):
    values = [event(expected=.2)]
    monkeypatch.setattr(macro, "_fetch", lambda *a: (values, []))
    with run_config({"data_cache_dir": str(tmp_path)}):
        macro.build_macro_event_context("2026-11-09")
        clock[0] = datetime(2026, 11, 10, 14, tzinfo=UTC)
        values[:] = [event(actual=.3, expected=.8)]
        macro.build_macro_event_context("2026-11-10")
        clock[0] = datetime(2026, 11, 11, 14, tzinfo=UTC)
        monkeypatch.setattr(macro, "_fetch", lambda *a: pytest.fail("historical run reached live calendar"))
        before = macro.build_macro_event_context("2026-11-09")
        after = macro.build_macro_event_context("2026-11-10")
        empty = macro.build_macro_event_context("2026-11-08")
    assert "Historical mode" in before and "0.8 %" not in before and "0.3 %" not in before
    assert "row captured 2026-11-09T12:00:00+00:00" in before
    assert "| 0.3 % | 0.8 % | 0.2 %" in after
    assert "unavailable/empty" in empty


def test_historical_pre_release_row_is_not_upgraded_to_released(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(macro, "_fetch", lambda *a: ([event()], []))
    with run_config({"data_cache_dir": str(tmp_path)}):
        macro.build_macro_event_context("2026-11-09")
        clock[0] = datetime(2026, 11, 11, 14, tzinfo=UTC)
        text = macro.build_macro_event_context("2026-11-10")
    assert "scheduled; actual/gap withheld" in text
    assert "row captured 2026-11-09" in text


def test_calendar_failure_and_corrupt_archive_do_not_abort_report(tmp_path, monkeypatch, clock):
    directory = tmp_path / macro.VERSION
    directory.mkdir()
    (directory / "bad.json").write_text('{"events":', encoding="utf-8")
    def fail(*args):
        raise RuntimeError("secret_key=do-not-print-me")
    monkeypatch.setattr(macro, "_fetch", fail)
    with run_config({"data_cache_dir": str(tmp_path)}):
        text = macro.build_macro_event_context("2026-11-09")
    assert "unreadable" in text and "Live calendar unavailable (RuntimeError)" in text
    assert "secret_key" not in text and "do-not-print-me" not in text


def test_estimated_timestamp_withholds_actual_and_gap(tmp_path, monkeypatch, clock):
    clock[0] = datetime(2026, 11, 10, 14, tzinfo=UTC)
    monkeypatch.setattr(macro, "_fetch", lambda *a: ([event(actual=.3, precise=False)], []))
    with run_config({"data_cache_dir": str(tmp_path)}):
        text = macro.build_macro_event_context("2026-11-10")
    assert "estimated timing; actual/gap withheld" in text and "0.3 %" not in text


def test_future_analysis_date_and_disabled_mode_do_not_fetch(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(macro, "_fetch", lambda *a: pytest.fail("must not fetch"))
    with run_config({"data_cache_dir": str(tmp_path)}):
        assert "Future analysis date" in macro.build_macro_event_context("2026-11-10")
    with run_config({"data_cache_dir": str(tmp_path), "macro_events_enabled": False}):
        assert "disabled" in macro.build_macro_event_context("2026-11-09")


def test_trading_economics_uses_forecast_not_teforecast(monkeypatch):
    monkeypatch.setenv("TRADING_ECONOMICS_API_KEY", "test-key")
    records = [dict(Country="United States", Event="CPI MoM", Date="2026-11-10T13:30:00",
                    Reference="Oct", Actual="0.3%", Forecast="", TEForecast="0.2%",
                    Previous="0.3%", Revised="0.4%", Unit="%", DateSpan="0")]
    seen = []
    monkeypatch.setattr(macro.requests, "get", lambda url, **kw: seen.append((url, kw)) or
                        SimpleNamespace(raise_for_status=lambda: None, json=lambda: records))
    events, _ = macro._fetch(datetime(2026, 11, 2).date(), datetime(2026, 11, 23).date(), "trading_economics")
    assert events[0]["expected"] is None and events[0]["actual"] == dict(value=.3, unit="%")
    assert events[0]["release_at"].endswith("+00:00") and events[0]["precise"]
    assert seen[0][1]["timeout"] == 20


def test_news_node_caches_packet_across_tool_rounds_and_appends_evidence(monkeypatch):
    from tradingagents.agents.analysts import news_analyst as news
    captures, prompts = [], []
    monkeypatch.setattr(news, "build_macro_event_context", lambda d: captures.append(d) or "macro-evidence")
    replies = [(AIMessage(content="", tool_calls=[dict(name="get_news", args={}, id="one")]), ""),
               (AIMessage(content="analysis"), "analysis")]
    def turn(prompt, llm, tools, messages):
        prompts.append(prompt.format_messages(messages=[])[0].content)
        return replies.pop(0)
    monkeypatch.setattr(news, "take_turn", turn)
    state = dict(company_of_interest="TQQQ", trade_date="2026-11-09", messages=[])
    node = news.create_news_analyst(None)
    first = node(state)
    assert not first["news_report"] and first["macro_event_context"] == "macro-evidence"
    second = node(dict(state, **first))
    assert captures == ["2026-11-09"] and all("macro-evidence" in p for p in prompts)
    assert second["news_report"].endswith("macro-evidence")


def test_archive_write_failure_is_visible_and_does_not_drop_live_values(tmp_path, monkeypatch, clock):
    (tmp_path / macro.VERSION).write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(macro, "_fetch", lambda *a: ([event()], []))
    with run_config({"data_cache_dir": str(tmp_path)}):
        text = macro.build_macro_event_context("2026-11-09")
    assert "Could not save this capture" in text
    assert "CPI MM, SA" in text


def test_conflicting_duplicates_unknown_times_and_foreign_rows_are_not_used(monkeypatch):
    rows = [dict(Event="CPI MM, SA", Region="US", **{"Event Time": "2026-11-10T13:30:00Z", "For": "Oct", "Actual": .3}),
            dict(Event="CPI MM, SA", Region="US", **{"Event Time": "2026-11-10T13:30:00Z", "For": "Oct", "Actual": .4}),
            dict(Event="CPI YY, NSA", Region="US", **{"Event Time": "invalid"}),
            dict(Event="CPI MM, SA", Region="CN", **{"Event Time": "2026-11-10T13:30:00Z"}),
            dict(Event="Core CPI MM, SA", Region="US", **{"Event Time": "2026-12-10T13:30:00Z"})]
    monkeypatch.setattr(yahoo, "fetch_us_calendar", lambda *a: (rows, []))
    events, warnings = macro._fetch(datetime(2026, 11, 2).date(), datetime(2026, 11, 23).date(), "yahoo")
    assert events == []
    assert "Conflicting duplicate" in " ".join(warnings)
    assert "invalid release timestamps" in " ".join(warnings)


@pytest.mark.parametrize("release,local", [("2026-11-10T13:30:00Z", "08:30 EST"),
                                          ("2026-07-10T12:30:00Z", "08:30 EDT")])
def test_calendar_uses_dst_aware_new_york_time(release, local):
    e = event(release=release)
    at = macro._timestamp(release)
    text = macro._format([e], [], at, at, at.date(), at.date(), "yahoo", [], False)
    assert local in text
