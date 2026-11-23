"""ETF routing, real tool suppression, point-in-time boundaries and arithmetic."""
import copy
from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd
import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from tradingagents.agents.context import build_instrument_context, get_instrument_context_from_state
from tradingagents.agents.analysts import fundamentals_analyst as fa
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo import etf
from tradingagents.instruments import resolve_instrument_profile


def test_tqqq_is_an_etf_without_vendor_metadata():
    profile = resolve_instrument_profile("tqqq")
    assert profile.kind == "etf" and profile.research_proxy == "QQQ"
    assert profile.daily_leverage == 3 and profile.daily_reset
    context = build_instrument_context("TQQQ")
    assert "Fund: ProShares UltraPro QQQ" in context
    assert "Instrument type: ETF" in context and "Company:" not in context
    assert "not a replacement for TQQQ's execution price" in context


def test_other_etf_uses_reported_type_without_inventing_leverage():
    profile = resolve_instrument_profile("SPY", identity={"quote_type": "ETF", "company_name": "SPDR"})
    assert profile.kind == "etf" and profile.daily_leverage is None and profile.research_proxy is None
    assert resolve_instrument_profile("NVDA").kind == "stock"
    assert resolve_instrument_profile("BTC-USD", "crypto").kind == "crypto"


def test_saved_profile_wins_over_other_run_global_settings(monkeypatch):
    from tradingagents.dataflows import config
    monkeypatch.setattr(config, "get_config", lambda: {"instrument_profiles": {"TQQQ": {"kind": "stock"}}})
    state = {"company_of_interest": "TQQQ", "trade_date": "2026-01-09",
             "instrument_profile": resolve_instrument_profile("TQQQ").to_dict()}
    assert "Instrument type: ETF" in get_instrument_context_from_state(state)


def test_explicit_etf_profile_is_supported():
    profile = resolve_instrument_profile("SQQQ", config={"instrument_profiles": {"SQQQ": {
        "kind": "etf", "research_proxy": "QQQ", "daily_leverage": -3, "daily_reset": True}}})
    assert profile.kind == "etf" and profile.daily_leverage == -3
    with pytest.raises(ValueError):
        resolve_instrument_profile("TQQQ", config={"instrument_profiles": {"TQQQ": {"ticker": "QQQ"}}})


@pytest.mark.parametrize("value", ["3", True, float("inf"), float("nan"), 0])
def test_invalid_leverage_is_rejected(value):
    with pytest.raises(ValueError):
        resolve_instrument_profile("TQQQ", config={"instrument_profiles": {"TQQQ": {"daily_leverage": value}}})


def test_reports_show_etf_profile_and_escape_untrusted_names():
    from tradingagents.reporting import _header
    from tradingagents.report_html import _run_details
    state = {"trade_date": "2026-11-09", "instrument_profile": resolve_instrument_profile("TQQQ").to_dict()}
    assert "Instrument type: ETF" in _header("TQQQ", state, None)
    assert "3.0x" in dict(_run_details(state, None))["Daily leverage"]
    state["instrument_profile"]["research_proxy"] = "<script>"
    assert "&lt;script&gt;" in dict(_run_details(state, None))["Instrument"]


def test_daily_compounding_is_not_three_times_cumulative_return():
    index, fund = etf.simulate_daily_leverage([0.02, -0.02], 3)
    assert index == pytest.approx(-0.0004)
    assert fund == pytest.approx(-0.0036)
    assert fund != pytest.approx(index * 3)
    with pytest.raises(ValueError):
        etf.simulate_daily_leverage([-0.5], 3)


@pytest.fixture
def evidence(monkeypatch):
    calls = []
    monkeypatch.setattr(etf, "get_config", lambda: {"etf_research_constituents": 1, "etf_research_horizon_sessions": 10})
    monkeypatch.setattr(etf, "is_historical", lambda day: day < "2026-11-09")
    monkeypatch.setattr(etf, "_now_utc", lambda: datetime(2026, 11, 9, 14, tzinfo=UTC))
    monkeypatch.setattr(etf, "build_verified_market_snapshot", lambda symbol, day, **k: calls.append(("market", symbol, day)) or "QQQ verified market evidence")
    holdings = pd.DataFrame({"Name": ["First", "Second"], "Holding Percent": [0.2, 0.1]}, index=["NVDA", "MSFT"])
    estimates = pd.DataFrame({"avg": [2.0, 3.0], "yearAgoEps": [1.0, -1.0],
                              "currency": ["USD", "USD"], "methodology": ["normalized", "normalized"]}, index=["0q", "+1q"])
    trend = pd.DataFrame({"current": [2.0, 3.0], "30daysAgo": [1.0, 2.0],
                         "currency": ["USD", "USD"], "methodology": ["normalized", "normalized"]}, index=["0q", "+1q"])
    dates = pd.DataFrame({"EPS Estimate": [2, 3]}, index=pd.DatetimeIndex(["2026-11-17 16:00", "2027-02-01 16:00"], tz="America/New_York"))
    class Ticker:
        def __init__(self, symbol):
            self.symbol = symbol
        def get_funds_data(self):
            calls.append(("holdings", self.symbol))
            return SimpleNamespace(top_holdings=holdings)
        def get_earnings_estimate(self):
            calls.append(("eps", self.symbol))
            return estimates.copy()
        def get_eps_trend(self):
            return trend.copy()
        def get_earnings_dates(self, limit=12):
            return dates.copy()
    monkeypatch.setattr(etf.yf, "Ticker", Ticker)
    return calls, holdings, estimates, trend, dates


def test_proxy_holdings_and_eps_are_bounded_and_labelled(evidence):
    calls, *_ = evidence
    report = etf.build_etf_research(resolve_instrument_profile("TQQQ"), "2026-11-09")
    assert ("market", "QQQ", "2026-11-09") in calls
    assert ("holdings", "QQQ") in calls
    assert ("eps", "NVDA") in calls and ("eps", "MSFT") not in calls
    assert "Listed weight coverage: 30.00%" in report
    assert "2026-11-17T16:00:00-05:00" in report and "2027-02-01" not in report
    assert "not TQQQ's derivative holdings" in report
    assert "reported-as-of date is unavailable" in report
    assert "+100.00%" in report and "nonpositive base" in report
    assert "NOT forecasts" in report and "no verified economic-release calendar" in report


def test_historical_run_never_reads_current_membership_or_estimates(evidence):
    calls, *_ = evidence
    report = etf.build_etf_research(resolve_instrument_profile("TQQQ"), "2026-01-09")
    assert calls == [("market", "QQQ", "2026-01-09")]
    assert "withheld" in report and "NVDA" not in report and "2026-11-09T" not in report


def test_missing_holdings_does_not_create_a_constituent_watchlist(evidence, monkeypatch):
    monkeypatch.setattr(etf.yf, "Ticker", lambda symbol: (_ for _ in ()).throw(VendorUnavailableError("offline")))
    report = etf.build_etf_research(resolve_instrument_profile("TQQQ"), "2026-11-09")
    assert "Missing or limited evidence" in report and "no membership inferred" in report
    assert "NVDA" not in report


def test_eps_currency_mismatch_is_not_used_for_revision(evidence):
    evidence[3].loc["0q", "currency"] = "EUR"
    report = etf.build_etf_research(resolve_instrument_profile("TQQQ"), "2026-11-09")
    assert "currency/methodology mismatch" in report


def test_etf_branch_does_not_bind_or_execute_corporate_tools(monkeypatch):
    seen = []
    monkeypatch.setattr(fa, "build_etf_research", lambda profile, day: "PACKET: missing weights explicitly")
    llm = RunnableLambda(lambda prompt: seen.append(prompt.to_string()) or AIMessage(content="ETF summary"))
    state = {"company_of_interest": "TQQQ", "trade_date": "2026-11-09", "messages": [("human", "TQQQ")]}
    out = fa.create_fundamentals_analyst(llm)(state)
    assert "ETF summary" in out["fundamentals_report"] and "PACKET" in out["fundamentals_report"]
    assert not out["messages"][0].tool_calls
    assert "No external tools are available" in seen[0]


def test_hallucinated_corporate_call_cannot_escape_etf_branch(monkeypatch):
    monkeypatch.setattr(fa, "build_etf_research", lambda *a: "PACKET")
    llm = RunnableLambda(lambda prompt: AIMessage(content="", tool_calls=[{
        "name": "get_balance_sheet", "args": {}, "id": "bad"}]))
    out = fa.create_fundamentals_analyst(llm)({"company_of_interest": "TQQQ", "trade_date": "2026-11-09", "messages": []})
    assert not out["messages"][0].tool_calls and "No tool was executed" in out["fundamentals_report"]


def test_etf_runs_through_real_graph_and_retains_profile(tmp_path, monkeypatch):
    from tests.test_graph_end_to_end import ScriptedModel, _Client
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph import trading_graph
    monkeypatch.setattr(fa, "build_etf_research", lambda *a: "QQQ evidence packet")
    monkeypatch.setattr(trading_graph, "resolve_instrument_identity", lambda *a: pytest.fail("known TQQQ must work without profile lookup"))
    model = ScriptedModel()
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda *a, **k: _Client(model))
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(data_cache_dir=str(tmp_path / "cache"), results_dir=str(tmp_path / "results"), memory_log_path=str(tmp_path / "memory.md"))
    graph = trading_graph.TradingAgentsGraph(selected_analysts=("fundamentals",), config=cfg)
    state, signal = graph.propagate("TQQQ", "2026-01-09")
    assert state["instrument_profile"]["kind"] == "etf" and state["research_rating"] == "Overweight"
    assert signal == "REVIEW"  # Fundamentals alone provide no verified execution-instrument close.
    assert "QQQ evidence packet" in state["fundamentals_report"]
