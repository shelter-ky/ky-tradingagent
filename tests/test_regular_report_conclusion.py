from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from tests.test_allocation_risk import BOOK, POLICY
from tradingagents.dataflows.config import run_config
from tradingagents.report_conclusion import market_coverage
from tradingagents.reporting import write_report_tree
from tradingagents.risk import risk_validation_node


def packet(latest, expected, *, ticker="TQQQ", as_of="2026-11-09", warning=""):
    content = (f"## Verified market data snapshot for {ticker}\n\n- Requested analysis date: {as_of}\n"
               f"- Latest trading row used: {latest}\n- Data source: recorded daily bars\n"
               f"- Latest data row: {latest}\n- Last completed scheduled US session: {expected}\n"
               f"{warning}\n### Latest verified OHLCV row\n")
    return ToolMessage(content, name="get_verified_market_snapshot", tool_call_id="test")


@pytest.mark.parametrize("messages,day,status", [
    ([packet("2026-11-06", "2026-11-09")], "2026-11-09", "STALE"),
    ([packet("2026-11-09", "2026-11-06")], "2026-11-09", "PROVISIONAL"),
    ([packet("2026-11-06", "2026-11-06", as_of="2026-11-08")], "2026-11-08", "COVERED"),
    ([packet("2026-11-09", "2026-11-09", warning="- WARNING: live refresh failed; using validated cached daily bars, not a live quote.")], "2026-11-09", "COVERED"),
    ([packet("2026-11-09", "2026-11-09", ticker="QQQ")], "2026-11-09", "UNVERIFIED"),
    ([packet("2026-11-06", "2026-11-06", as_of="2026-11-06")], "2026-11-09", "UNVERIFIED"),
    ([AIMessage(packet("2026-11-09", "2026-11-09").content)], "2026-11-09", "UNVERIFIED"),
    ([packet("2026-11-09", "2026-11-09"), ToolMessage("DATA_UNAVAILABLE", name="get_verified_market_snapshot", tool_call_id="retry")], "2026-11-09", "UNVERIFIED"),
    ([], "2026-11-09", "UNVERIFIED"),
])
def test_only_a_matching_completed_tool_snapshot_establishes_coverage(messages, day, status):
    assert market_coverage(messages, "TQQQ", day)["status"] == status


@pytest.mark.parametrize("latest,limits,label,signal,sized", [
    ("2026-11-06", POLICY, "等待行情核验", "REVIEW", False),
    ("2026-11-06", {}, "等待行情核验", "REVIEW", False),
    ("2026-11-09", {}, "仅供研究，仓位待核验", "Buy", False),
    ("2026-11-09", dict(POLICY, max_position_weight=.01), "仓位校验未通过", "Buy", False),
    ("2026-11-09", POLICY, "仓位已通过配置校验", "Buy", True),
])
def test_public_outputs_share_only_the_checked_conclusion(tmp_path, latest, limits, label, signal, sized):
    day = "2026-11-09"
    state = dict(company_of_interest="TQQQ", trade_date=day, final_rating="Buy",
                 portfolio_data=deepcopy(BOOK), allocation_proposal={"target_weight":.15, "stop_loss":90},
                 market_coverage=market_coverage([packet(latest, day)], "TQQQ", day),
                 final_trade_decision="Rating: Buy\n建议动用 0.5 倍战术仓位",
                 market_report="留存的研究依据", trader_investment_plan="供复核的原始交易员意见")
    with run_config({"risk_limits":limits}):
        state.update(risk_validation_node(state))
    assert state["final_rating"] == signal
    assert bool(state["validated_allocation"]) == sized
    if latest != day:
        assert state["risk_validation"]["calculations"] == {}
    report = write_report_tree(state, "TQQQ", tmp_path)
    markdown = report.read_text(encoding="utf-8")
    html = (tmp_path / "complete_report.html").read_text(encoding="utf-8")
    decision = (tmp_path / "5_portfolio/decision.md").read_text(encoding="utf-8")
    for output in (decision, markdown.split("## I.")[0], html.split("</header>")[0]):
        assert label in output and "0.5 倍战术仓位" not in output
        assert ("目标 15 股" in output) == sized
        if latest != day:
            assert latest in output and day in output
            assert "Rating: Buy" not in output
    assert state["decision_summary"] == decision
    assert "留存的研究依据" in markdown and "供复核的原始交易员意见" in html


def test_cli_waits_for_checked_decision_before_showing_final_section(tmp_path, monkeypatch, capsys):
    from tests.test_cli_memory_log import _FakeBuffer, _FakeGraph, _run_cli

    sections = []
    monkeypatch.setattr(_FakeBuffer, "update_report_section", lambda self, key, value: sections.append((key, value)))
    raw = "模型原文，建议 0.5 倍仓位"
    final = "等待行情核验，缺少最近已完成交易日的数据。"

    class Replay(_FakeGraph):
        def stream_run(self, graph_input, **kwargs):
            draft = dict(risk_debate_state={"aggressive_history":"模拟讨论"}, final_trade_decision=raw)
            yield [], draft
            yield [], {**draft, "final_trade_decision": final, "decision_summary": final, "final_rating": "REVIEW"}

    _run_cli(monkeypatch, tmp_path, Replay())
    rendered = [value for key, value in sections if key == "final_trade_decision"]
    assert rendered and all(final in item and raw not in item for item in rendered)
    shown = capsys.readouterr().out
    assert "等待行情核验" in shown and raw not in shown
