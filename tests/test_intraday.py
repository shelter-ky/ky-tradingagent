"""Meaningful gates for a live decision: time, identity, gaps, model and startup."""
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest
from typer.testing import CliRunner

import tradingagents.dataflows.intraday as data
from tradingagents.intraday import IntradayEngine, render_report

NOW = pd.Timestamp("2026-11-12T15:05:30Z")


def bars(n=35, slope=0.1, date="2026-11-12", tz="America/New_York"):
    index = pd.date_range(f"{date} 09:30", periods=n, freq="min", tz=tz)
    close = [100 + i * slope for i in range(n)]
    return pd.DataFrame({"Open": close, "High": [x + .02 for x in close],
                         "Low": [x - .02 for x in close], "Close": close, "Volume": 100}, index=index)


def snapshot(ticker="TQQQ", **kw):
    return data.snapshot_from_bars(ticker, bars(**kw), NOW)


def opinion(packet):
    return {"action": packet["rule_action"], "reason": "分钟趋势与 QQQ 同向，仅观察。", "risks": ["行情可能延迟"]}


@pytest.mark.parametrize("stamp,state", [
    ("2026-11-12T14:29:59Z", "PREMARKET"), ("2026-11-12T14:30:00Z", "REGULAR"),
    ("2026-11-12T20:59:59Z", "REGULAR"), ("2026-11-12T21:00:00Z", "CLOSED"),
    ("2026-07-08T13:30:00Z", "REGULAR"),  # daylight saving
    ("2026-11-26T16:00:00Z", "CLOSED"),  # Thanksgiving
    ("2026-11-27T18:00:00Z", "CLOSED"),  # early close 13:00 NY
    ("2026-11-14T16:00:00Z", "CLOSED"),
])
def test_session_calendar_and_dst(stamp, state):
    assert data.market_session(pd.Timestamp(stamp)) == state


def test_active_minute_is_excluded_and_vwap_uses_complete_session_only():
    frame = bars(n=36)
    frame.iloc[-1] = [999, 1000, 998, 999, 1000000]
    snap = data.snapshot_from_bars("TQQQ", frame, NOW)
    assert snap.bars == 35 and snap.price == pytest.approx(103.4)
    assert snap.vwap == pytest.approx(101.7)
    assert snap.volume == 3500 and snap.bar_end == "2026-11-12T15:05:00+00:00"


@pytest.mark.parametrize("case", ["gap", "no_open", "duplicate", "naive", "nan_tail", "infinite",
                                  "negative_volume", "negative_close", "invalid_high", "zero_volume",
                                  "future", "stale", "short", "nonminute"])
def test_invalid_data_cannot_emit_snapshot(case):
    frame = bars()
    if case == "gap": frame = frame.drop(frame.index[10])
    if case == "no_open": frame = frame.iloc[1:]
    if case == "duplicate": frame = pd.concat([frame, frame.iloc[-1:]])
    if case == "naive": frame.index = frame.index.tz_localize(None)
    if case == "nan_tail": frame.iloc[-1, 3] = float("nan")
    if case == "infinite": frame.iloc[-1, 3] = float("inf")
    if case == "negative_volume": frame.iloc[-1, 4] = -1
    if case == "negative_close": frame.iloc[-1, 3] = -1
    if case == "invalid_high": frame.iloc[-1, 1] = 1
    if case == "zero_volume": frame["Volume"] = 0
    if case == "future": frame.index = frame.index + pd.Timedelta(hours=2)
    if case == "stale": frame = frame.iloc[:-5]
    if case == "short": frame = frame.iloc[:20]
    if case == "nonminute": frame.index = frame.index + pd.Timedelta(seconds=1)
    with pytest.raises(ValueError):
        data.snapshot_from_bars("TQQQ", frame, NOW)


def test_yesterday_and_unsupported_markets_are_not_current_prices():
    with pytest.raises(ValueError): data.snapshot_from_bars("TQQQ", bars(date="2026-11-11"), NOW)
    with pytest.raises(ValueError): data.snapshot_from_bars("0700.HK", bars(), NOW)


def test_fetch_is_unadjusted_minute_data_with_verified_identity(monkeypatch):
    args = []
    obj = SimpleNamespace(history=lambda **kw: args.append(kw) or bars(),
                          history_metadata={"symbol": "TQQQ", "currency": "USD",
                                            "exchangeTimezoneName": "America/New_York", "instrumentType": "ETF"})
    monkeypatch.setattr(data.yf, "Ticker", lambda ticker: obj)
    assert data.fetch_snapshot("TQQQ", clock=lambda: NOW).price > 0
    assert args[0]["auto_adjust"] is False and args[0]["interval"] == "1m"
    assert args[0]["prepost"] is False and args[0]["timeout"] == 10
    obj.history_metadata["symbol"] = "QQQ"
    with pytest.raises(ValueError, match="身份"):
        data.fetch_snapshot("TQQQ", clock=lambda: NOW)


def engine(*, fetch=snapshot, clock=lambda: NOW, review=opinion):
    return IntradayEngine("TQQQ", {}, fetch=fetch, clock=clock, review=review)


def test_tqqq_uses_qqq_confirmation_and_only_reviews_changed_packets():
    fetched, reviewed = [], []
    def fetch(t): fetched.append(t); return snapshot(t)
    def review(p): reviewed.append(p); return opinion(p)
    e = engine(fetch=fetch, review=review)
    first, second = e.step(), e.step()
    assert fetched == ["TQQQ", "QQQ", "TQQQ", "QQQ"] and len(reviewed) == 1
    assert first["action"] == "WATCH_LONG" and second["model_status"] == "REUSED_SAME_PACKET"
    assert first["sizing"]["validated_allocation"] is None
    assert "max_position_weight" in first["sizing"]["missing"]


def test_qqq_is_not_fetched_twice_for_qqq():
    seen = []
    e = IntradayEngine("QQQ", {}, fetch=lambda t: seen.append(t) or snapshot(t), clock=lambda: NOW, review=opinion)
    assert e.step()["action"] == "WATCH_LONG" and seen == ["QQQ"]


def test_proxy_conflict_or_missing_proxy_waits_without_model():
    for fail in (False, True):
        calls = []
        def fetch(t):
            if t == "QQQ" and fail: raise OSError("rate limited")
            return snapshot(t, slope=-.1 if t == "QQQ" else .1)
        result = engine(fetch=fetch, review=lambda p: calls.append(p) or opinion(p)).step()
        assert result["action"] == "WAIT" and not calls


def test_unsynchronised_proxy_waits_without_model():
    def fetch(t):
        s = snapshot(t)
        return replace(s, bar_end=(pd.Timestamp(s.bar_end) - pd.Timedelta(minutes=2)).isoformat()) if t == "QQQ" else s
    assert engine(fetch=fetch, review=lambda p: pytest.fail("called model")).step()["status"] == "UNAVAILABLE"


def test_closed_market_does_not_fetch_or_call_model():
    e = engine(clock=lambda: pd.Timestamp("2026-11-12T21:00Z"),
               fetch=lambda t: pytest.fail("fetched"), review=lambda p: pytest.fail("called"))
    assert e.step()["action"] == "WAIT"


@pytest.mark.parametrize("action", ["WAIT", "WATCH_RISK"])
def test_model_cannot_override_technical_direction(action):
    result = engine(review=lambda p: {"action": action, "reason": "测试", "risks": ["未知事件"]}).step()
    assert result["action"] == "WAIT"


@pytest.mark.parametrize("answer", [{"action": "BUY", "reason": "a", "risks": ["a"]},
                                   {"action": "WATCH_LONG", "reason": "a", "risks": ["a"], "quantity": 100}])
def test_malformed_model_output_never_becomes_a_signal(answer):
    result = engine(review=lambda p: answer).step()
    assert result["action"] == "WAIT" and result["status"] == "UNAVAILABLE"


def test_model_delay_and_close_rechecked_after_response():
    for late in (NOW + pd.Timedelta(minutes=5), pd.Timestamp("2026-11-12T21:00Z")):
        current = [NOW]
        def review(p): current[0] = late; return opinion(p)
        result = engine(clock=lambda: current[0], review=review).step()
        assert result["action"] == "WAIT" and "model" not in result


def test_error_drops_old_opinion_and_does_not_leak_provider_message():
    failed = [False]
    def fetch(t):
        if failed[0]: raise RuntimeError("secret-token-example")
        return snapshot(t)
    e = engine(fetch=fetch)
    assert e.step()["action"] == "WATCH_LONG"
    failed[0] = True
    result = e.step()
    assert result["action"] == "WAIT" and e._last_opinion is None
    assert "secret-token" not in str(result)


def test_display_expires_signal_during_refresh_wait():
    result = engine().step()
    text = render_report(result, NOW + pd.Timedelta(minutes=5))
    assert "**当前：等待**" in text and "观察条件：" not in text
    assert "模型提示：" not in text


def test_startup_menu_modes_and_cancellation(monkeypatch):
    import cli.intraday as cli
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli.questionary, "select", lambda *a, **k: SimpleNamespace(ask=lambda: "intraday"))
    assert cli.choose_mode() == "intraday"
    assert cli.choose_mode("analysis") == "analysis"
    monkeypatch.setattr(cli.questionary, "select", lambda *a, **k: SimpleNamespace(ask=lambda: None))
    with pytest.raises(Exception): cli.choose_mode()


def test_cli_routes_intraday_and_validates_options(monkeypatch):
    import cli.main as main
    import cli.intraday as cli
    calls = []
    monkeypatch.setattr(cli, "run_intraday", lambda **kw: calls.append(kw))
    monkeypatch.setattr(main, "run_analysis", lambda **kw: pytest.fail("regular path"))
    runner = CliRunner()
    result = runner.invoke(main.app, ["--mode", "intraday", "--ticker", "TQQQ", "--once"])
    assert result.exit_code == 0, result.output
    assert calls[0]["flags"]["ticker"] == "TQQQ" and calls[0]["once"]
    for args in (["--mode", "invalid"], ["--mode", "intraday", "--interval", "2"],
                 ["--mode", "intraday", "--date", "2026-11-11"]):
        assert runner.invoke(main.app, args).exit_code != 0


def test_once_saves_report_without_running_full_research(tmp_path, monkeypatch):
    import cli.intraday as cli
    from cli.models import AnalystType
    monkeypatch.setattr(cli, "get_user_selections", lambda flags: {"ticker": "TQQQ", "analysts": [AnalystType.MARKET]})
    monkeypatch.setattr(cli, "_build_run_config", lambda *a: {"results_dir": str(tmp_path)})
    monkeypatch.setattr(cli, "IntradayEngine", lambda *a: engine())
    result = cli.run_intraday(once=True)
    files = list(tmp_path.rglob("latest.json"))
    assert result["action"] == "WATCH_LONG" and len(files) == 1
    assert files[0].with_suffix(".md").exists()


def test_changed_bad_portfolio_file_blocks_model(tmp_path, monkeypatch):
    import cli.intraday as cli
    monkeypatch.setattr(cli, "get_user_selections", lambda flags: {"ticker": "TQQQ"})
    monkeypatch.setattr(cli, "_build_run_config", lambda *a: {"results_dir": str(tmp_path)})
    monkeypatch.setattr(cli, "IntradayEngine", lambda *a: engine(review=lambda p: pytest.fail("model")))
    result = cli.run_intraday(once=True, portfolio_path=tmp_path / "missing.json")
    assert result["status"] == "INPUT_ERROR" and result["action"] == "WAIT"


def test_intraday_selection_skips_daily_steps_and_unused_deep_provider(monkeypatch):
    import cli.selections as choices
    monkeypatch.setattr(choices.updates, "enabled", lambda: False)
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "openai")
    monkeypatch.setenv("TRADINGAGENTS_QUICK_THINK_LLM", "test-fast")
    monkeypatch.setenv("TRADINGAGENTS_OUTPUT_LANGUAGE", "Chinese")
    monkeypatch.setattr(choices, "ensure_signed_in", lambda p: None)
    monkeypatch.setattr(choices, "ensure_api_key", lambda p: None)
    monkeypatch.setattr(choices, "DEFAULT_CONFIG", {**choices.DEFAULT_CONFIG,
                        "quick_think_provider": None, "deep_think_provider": "anthropic"})
    for method in ("get_analysis_date", "select_analysts", "select_research_depth", "select_deep_thinking_agent"):
        monkeypatch.setattr(choices, method, lambda *a, **k: pytest.fail("daily setup prompted"))
    selected = choices._prompt_selections({}, {"ticker": "TQQQ", "_intraday": True})
    assert selected["research_depth"] == 1
    assert selected["analysis_date"] == data.utc_now().tz_convert("America/New_York").date().isoformat()


def test_intraday_does_not_overwrite_regular_analysis_preferences(monkeypatch):
    import cli.selections as choices
    original = {"analysts": ["market", "news"], "research_depth": 5, "deep_think_llm": "old-deep"}
    saved = []
    monkeypatch.setattr(choices, "load_last_run", lambda: original.copy())
    monkeypatch.setattr(choices, "save_last_run", lambda p: saved.append(p))
    monkeypatch.setattr(choices, "_prompt_selections", lambda *a: {"analysts": ["market"], "research_depth": 1,
                                                                "deep_think_llm": "new-fast", "quick_think_llm": "new-fast"})
    choices.get_user_selections({"_intraday": True})
    assert all(saved[0][k] == v for k, v in original.items())
    assert saved[0]["quick_think_llm"] == "new-fast"


def test_stopping_loop_marks_saved_report_stopped(tmp_path, monkeypatch):
    import json
    import cli.intraday as cli
    from contextlib import nullcontext
    monkeypatch.setattr(cli, "get_user_selections", lambda flags: {"ticker": "TQQQ"})
    monkeypatch.setattr(cli, "_build_run_config", lambda *a: {"results_dir": str(tmp_path)})
    monkeypatch.setattr(cli, "IntradayEngine", lambda *a: engine())
    monkeypatch.setattr(cli, "Live", lambda **kw: nullcontext(SimpleNamespace(update=lambda *a, **k: None)))
    def interrupt(*a): raise KeyboardInterrupt()
    monkeypatch.setattr(cli.time, "sleep", interrupt)
    cli.run_intraday()
    result = json.loads(next(tmp_path.rglob("latest.json")).read_text(encoding="utf-8"))
    assert result["action"] == "WAIT" and result["status"] == "STOPPED" and result["valid_until"] is None


def test_new_minute_and_changed_account_trigger_new_model_review():
    from tradingagents.portfolio import PortfolioContext
    calls, revision = [], [0]
    def fetch(t):
        return replace(snapshot(t), price=snapshot(t).price + revision[0])
    e = engine(fetch=fetch, review=lambda p: calls.append(p) or opinion(p))
    e.step()
    revision[0] = .01
    e.step()
    e.step(portfolio=PortfolioContext(cash=1000, currency="USD"))
    assert len(calls) == 3


def test_model_request_is_single_quick_tier_bounded_and_structured(monkeypatch):
    import tradingagents.intraday as module
    calls = []
    def create(config, tier, **kwargs):
        calls.append((config, tier, kwargs))
        structured = SimpleNamespace(invoke=lambda packet: opinion({"rule_action": "WATCH_LONG"}))
        return SimpleNamespace(get_llm=lambda: SimpleNamespace(with_structured_output=lambda schema: structured))
    monkeypatch.setattr(module, "create_tier_client", create)
    review = module.model_reviewer({"llm_provider": "openai", "llm_max_retries": 5})
    assert review({})["action"] == "WATCH_LONG"
    assert calls[0][1] == "quick" and calls[0][0]["llm_max_retries"] == 0 and calls[0][2]["timeout"] == 30
