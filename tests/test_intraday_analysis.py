import json
from dataclasses import replace
from itertools import product

import pandas as pd
import pytest

from cli.intraday import _save
from tests.test_intraday import NOW, engine, opinion, snapshot
from tradingagents.intraday import IntradayEngine, render_report, technical_action


def test_all_factor_combinations_match_the_original_strict_rule():
    base = snapshot()
    for vwap_side, ema_side, momentum_side in product((-1, 0, 1), repeat=3):
        snap = replace(base, vwap=100, price=100+vwap_side, ema20=100, ema9=100+ema_side, momentum5_pct=momentum_side)
        expected = {(1, 1, 1): "WATCH_LONG", (-1, -1, -1): "WATCH_RISK"}.get((vwap_side, ema_side, momentum_side), "WAIT")
        assert technical_action(snap) == expected


def test_disagreement_is_explained_from_verified_snapshots_without_model():
    base = snapshot()
    def fetch(ticker):
        offset = 1 if ticker == "TQQQ" else -1
        return replace(base,ticker=ticker,price=100+offset,vwap=100,ema9=100+offset,ema20=100,momentum5_pct=offset)
    result = engine(fetch=fetch,review=lambda p: pytest.fail("model called")).step()
    a = result["analysis"]
    assert result["action"] == "WAIT" and "未满足双标的同向条件" in a["alignment"]
    assert [s["rule_action"] for s in a["symbols"]] == ["WATCH_LONG","WATCH_RISK"]
    assert a["symbols"][0]["values_pct"] == {"price_vwap":1,"ema_spread":1,"momentum5":1}
    assert all(c["kind"]=="FIRST" for c in a["changes"].values())
    for compact in (False,True):
        report = render_report(result,NOW,compact=compact)
        assert "TQQQ 三项偏强，QQQ 三项偏弱" in report and "与上一轮相比" in report
    assert "仍需 QQQ 收盘高于 VWAP" in render_report(result,NOW)


def test_equal_factor_and_single_instrument():
    base = snapshot("QQQ")
    snap = replace(base,price=base.vwap)
    result = IntradayEngine("QQQ",{},fetch=lambda ticker:snap,clock=lambda:NOW).step()
    a = result["analysis"]
    assert set(a["changes"]) == {"QQQ"} and result["rule_action"] == "WAIT"
    assert "未形成一致方向" in a["alignment"] and "价格相对 VWAP +0.000%（持平）" in render_report(result,NOW)


def test_round_changes_revisions_and_market_time_regression(tmp_path):
    clock = [NOW]
    base = snapshot()
    source = [replace(base,price=101,vwap=100,ema9=101,ema20=100,momentum5_pct=1)]
    e = engine(fetch=lambda t: replace(source[0],ticker=t),clock=lambda:clock[0])
    e.step()
    repeat = e.step()
    assert repeat["analysis"]["changes"]["TQQQ"]["kind"] == "UNCHANGED"
    assert repeat["model_status"] == "REUSED_SAME_PACKET"
    source[0] = replace(source[0],momentum5_pct=-1)
    # Same bar, revised momentum crosses zero. Do not call it a new minute.
    revised = e.step()
    revision = revised["analysis"]["changes"]["TQQQ"]
    assert "观察结果由买入条件观察变为等待" in render_report(revised, NOW)
    assert revision["kind"] == "REVISED" and revision["momentum_change_pp"] == -2
    assert revision["side_changes"] == {"momentum5":[1,-1]}
    older = source[0]
    clock[0] += pd.Timedelta(minutes=2)
    end = pd.Timestamp(base.bar_end)+pd.Timedelta(minutes=2)
    source[0] = replace(older,bar_end=end.isoformat(),price=102)
    result = e.step()
    change = result["analysis"]["changes"]["TQQQ"]
    assert change["kind"] == "NEW" and change["elapsed_minutes"] == 2
    assert change["price_change_pct"] == pytest.approx(100/101)
    text = render_report(result,clock[0])
    _save(tmp_path,result,text)
    assert json.loads((tmp_path/"latest.json").read_text(encoding="utf-8"))["analysis"] == result["analysis"]
    assert "距上次行情 2 分钟" in (tmp_path/"latest.md").read_text(encoding="utf-8")
    source[0] = older  # Still within 180 seconds but earlier than last accepted bar.
    for _ in range(2):
        rejected = e.step()
        assert rejected["action"] == "WAIT" and "时间回退" in rejected["reason"] and "analysis" not in rejected
    source[0] = replace(older,bar_end=end.isoformat())
    assert e.step()["analysis"]["changes"]["TQQQ"]["kind"] == "FIRST"


def test_no_previous_session_comparison_or_expired_analysis():
    clock = [NOW]
    base = snapshot()
    def fetch(ticker):
        return replace(base,ticker=ticker,bar_end=clock[0].floor("min").isoformat())
    e = engine(fetch=fetch,clock=lambda:clock[0])
    first = e.step()
    for compact in (False,True):
        expired = render_report(first,NOW+pd.Timedelta(minutes=3),compact=compact)
        assert "**当前：等待**" in expired and "## 盘中分析" not in expired and "与上一轮相比" not in expired
    # Rendering an archived pre-analysis result still works.
    legacy = {k:v for k,v in first.items() if k!="analysis"}
    assert "完整分钟线收盘" in render_report(legacy,NOW)
    clock[0] += pd.Timedelta(days=1)
    assert e.step()["analysis"]["changes"]["TQQQ"]["kind"] == "FIRST"


def test_model_can_veto_aligned_evidence_and_errors_reset_the_comparison():
    trigger = [False]
    def review(packet):
        if trigger[0]:
            raise RuntimeError("review unavailable")
        return {**opinion(packet),"action":"WAIT"}
    values = [snapshot()]
    e = engine(fetch=lambda t:replace(values[0],ticker=t),review=review)
    first = e.step()
    assert first["action"] == "WAIT" and first["rule_action"] == "WATCH_LONG"
    assert "两者同向" in first["analysis"]["alignment"]
    assert "模型复核后本轮仍为等待" in render_report(first, NOW)
    values[0] = replace(values[0],price=values[0].price+.01)
    trigger[0] = True
    assert "analysis" not in e.step()
    trigger[0] = False
    assert e.step()["analysis"]["changes"]["QQQ"]["kind"] == "FIRST"
