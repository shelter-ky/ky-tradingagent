"""Startup mode menu and serial minute decision loop (Ctrl+C to stop)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from uuid import uuid4

import questionary
import typer
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel

from cli.display import console
from cli.run import _build_run_config
from cli.selections import get_user_selections
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.intraday import is_us_equity, utc_now
from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.intraday import IntradayEngine, render_report
from tradingagents.portfolio import load_portfolio
from tradingagents.risk import load_risk_limits
from tradingagents.dataflows.intraday import fetch_regular_bars, aware_utc
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.forward_book import ForwardBook
from tradingagents.opening import OpeningEngine
from tradingagents.opening_rule import STRATEGY


def choose_mode(mode=None):
    if mode is not None:
        if mode not in {"analysis", "intraday", "opening0935"}:
            console.print("[red]--mode 可选 analysis、intraday 或 opening0935。[/red]")
            raise typer.Exit(code=1)
        return mode
    # Existing scripts/piped calls retain their regular-analysis behavior.
    if not (sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty()):
        return "analysis"
    answer = questionary.select(
        "请选择启动模式：",
        choices=[questionary.Choice("常规分析（完整研究报告）", value="analysis"),
                 questionary.Choice("盘中分析与实时决策（分钟趋势）", value="intraday"),
                 questionary.Choice("盘中决策（09:35 VXN 分批减仓／前向模拟）", value="opening0935")],
        default="analysis",
    ).ask()
    if answer is None:
        raise typer.Exit(code=0)
    return answer


def _save(directory, result, text, *, archive=True):
    stamp = utc_now().strftime("%Y%m%d_%H%M%S_%f")
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if archive:
        (directory / f"{stamp}.json").write_text(payload, encoding="utf-8")
    for name, content in (("latest.json", payload), ("latest.md", text)):
        replace_file(directory / name, lambda path, value=content: Path(path).write_text(value, encoding="utf-8"))


def run_intraday(*, flags=None, portfolio=None, portfolio_path=None, risk_limits=None,
                 risk_limits_path=None, interval=60, once=False, strategy="intraday"):
    flags = flags or {}
    if not 60 <= interval <= 3600:
        raise ValueError("刷新间隔需要在 60～3600 秒之间")
    if strategy != "opening0935" and not (sys.stdin and sys.stdin.isatty()):
        import os
        gaps = [key for key in ("TRADINGAGENTS_LLM_PROVIDER", "TRADINGAGENTS_OUTPUT_LANGUAGE") if not os.environ.get(key)]
        if flags.get("ticker") is None:
            gaps.append("--ticker")
        if not (os.environ.get("TRADINGAGENTS_QUICK_THINK_LLM") or os.environ.get("TRADINGAGENTS_DEEP_THINK_LLM")):
            gaps.append("TRADINGAGENTS_QUICK_THINK_LLM")
        if gaps:
            console.print("无终端可回答设置问题，请设置：" + "、".join(gaps), markup=False)
            raise typer.Exit(code=1)
    if strategy=="opening0935":
        symbol = flags.get("ticker") or (questionary.select("选择回放与观察标的", choices=["TQQQ","QQQ"]).ask()
                                         if sys.stdin.isatty() else "TQQQ")
        if not symbol:
            raise typer.Exit(code=0)
        selections = {"ticker":symbol.upper()}
        if selections["ticker"] not in ("TQQQ","QQQ"):
            console.print("09:35 策略仅支持 TQQQ、QQQ。")
            raise typer.Exit(code=1)
    else:
        selections = get_user_selections({**flags, "_intraday": True})
    if not is_us_equity(selections["ticker"]):
        console.print("[red]盘中模式当前支持美股／美股 ETF，例如 TQQQ、QQQ；该代码暂不支持。[/red]")
        raise typer.Exit(code=1)
    config = DEFAULT_CONFIG if strategy=="opening0935" else _build_run_config(selections, False)
    book = ForwardBook(Path(config["results_dir"])/"forward"/"observations.sqlite3")
    ticker = selections["ticker"]
    engine = OpeningEngine(ticker,book) if strategy=="opening0935" else IntradayEngine(ticker, config)
    strategy_id = STRATEGY if strategy=="opening0935" else "minute-trend-v1"
    directory = Path(config["results_dir"]) / safe_ticker_component(ticker) / strategy / (utc_now().strftime("%Y%m%d_%H%M%S_") + uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    console.print("09:35 分批规则按美东交易日运行，初始参考目标为满仓。无需模型。" if strategy=="opening0935"
                  else "盘中分钟趋势：仅新行情触发模型审查。",markup=False)
    console.print("行情可能延迟。前向模拟每次独立参考 7,000 美元，每单 5 美元，Ctrl+C 停止。",markup=False)
    console.print(f"报告目录：{directory}", markup=False)
    result = None
    try:
        while True:
            input_error = None
            try:
                if portfolio_path:
                    portfolio = load_portfolio(portfolio_path)
                if risk_limits_path:
                    risk_limits = load_risk_limits(risk_limits_path)
            except ValueError as exc:
                input_error = str(exc)
            if input_error:
                # A changed/unreadable book must not reuse a previously good one.
                result = {"version": "intraday-v1", "ticker": selections["ticker"],
                          "generated_at": utc_now().isoformat(), "action": "WAIT", "status": "INPUT_ERROR",
                          "session": "UNKNOWN", "reason": input_error, "snapshots": [],
                          "model_status": "NOT_CALLED", "valid_until": None,
                          "sizing": {"status": "NEEDS_INPUT", "reason": "修复账户／风控文件后重试。", "missing": [], "validated_allocation": None},
                          "limitations": ["本轮输入不可用，旧信号已停止展示。"]}
                engine.reset_observations()
            else:
                console.print("正在获取本轮分钟行情／审查条件……", markup=False)
                result = engine.step(portfolio=portfolio, limits=risk_limits)
            result["strategy_id"] = strategy_id
            result["forward_events"] = book.recent(strategy_id,ticker)
            # Publish before querying outcome prices; a slow follow-up cannot backdate a fill.
            _save(directory,result,render_report(result),archive=False)
            published = utc_now()
            if result.get("valid_until") and published < aware_utc(result["valid_until"]):
                book.record({**result,"published_at":published.isoformat()})
            if book.pending(strategy_id,ticker):
                try:
                    frame = fetch_regular_bars(ticker,period="5d")
                    book.settle(strategy_id,ticker,frame,utc_now())
                except Exception as exc:
                    result["forward_note"]=f"后续行情暂不可用（{type(exc).__name__}），将重试。"
            result["forward_events"]=book.recent(strategy_id,ticker)
            text = render_report(result)
            _save(directory, result, text)
            if once:
                console.print(Markdown(text))
                return result
            # Re-render during the delay so a signal expires even before the
            # next network request. No background/overlapping model requests.
            with Live(console=console, screen=True, auto_refresh=False) as live:
                delay=interval
                if strategy=="opening0935":
                    # Wake at the next minute boundary even if the preceding request took time.
                    delay=max(1,61-utc_now().second)
                for remaining in range(delay, 0, -1):
                    live.update(Panel(Markdown(render_report(result, compact=True)), title=f"下轮刷新 {remaining}s | Ctrl+C 停止"), refresh=True)
                    time.sleep(1)
    except KeyboardInterrupt:
        if result is not None:
            result.update(action="WAIT", status="STOPPED", valid_until=None,
                          generated_at=utc_now().isoformat(), reason="监测已停止，之前的信号已撤下。")
            result.pop("model", None)
            result.pop("analysis", None)
            _save(directory, result, render_report(result))
        console.print("\n盘中监测已停止。没有提交或撤销任何委托。", markup=False)
