import json
from dataclasses import replace

import pandas as pd
import pytest

from tests.test_intraday import opinion, snapshot
from tradingagents.intraday import IntradayEngine, render_report
from tradingagents.intraday_sell import _reference, sell_lines


def market(ticker="TQQQ", start="2026-11-23T16:19:00Z", review=opinion):
    state = {"end":pd.Timestamp(start), "risk":False, "skew":0}
    base = snapshot()

    def fetch(t):
        offset = pd.Timedelta(minutes=state["skew"] if t=="QQQ" else 0)
        end = state["end"] + offset
        side = -1 if state["risk"] else 1
        return replace(base,ticker=t,bar_start=(end-pd.Timedelta(minutes=1)).isoformat(),
                       bar_end=end.isoformat(),price=100+side,vwap=100,ema9=100+side,
                       ema20=100,momentum5_pct=side)

    return IntradayEngine(ticker,{},fetch=fetch,clock=lambda:state["end"]+pd.Timedelta(seconds=10),review=review), state


@pytest.fixture(autouse=True)
def synthetic_calibration(tmp_path, monkeypatch):
    """Tiny invented cohort to exercise fee labels, date bounds and row filtering."""
    row = dict(n=2, days=2, down_saved=3.0, rebound_missed=7.0,
               average_net=-2.0, profitable=1, sizes={"800": {"average_net": -2.6}})
    horizons = {key: row for key in ("5", "15", "30", "60", "收盘")}
    reference = dict(version=2, ticker="TQQQ", start="2026-11-19", end="2026-11-20",
                     session_minutes=390, sessions=2, unit_usd=1000, sale_fee_usd=5,
                     cohorts={key: horizons for key in ("all", "11:00", "15:30")})
    path = tmp_path / "synthetic.json"
    path.write_text(json.dumps(reference), encoding="utf-8")
    monkeypatch.setenv("TRADINGAGENTS_SELL_CALIBRATION", str(path))
    return path


def test_load_user_reference_and_handle_missing(synthetic_calibration):
    assert _reference()["cohorts"]["all"]["60"]["n"] == 2
    synthetic_calibration.unlink()
    eng, state = market()
    state["risk"] = True
    assert eng.step()["analysis"]["sell_impact"]["status"] == "UNAVAILABLE"


def test_confirmed_risk_only_and_episode_does_not_restart_or_leak():
    eng,state=market()
    assert "sell_impact" not in eng.step()["analysis"]
    state["end"]+=pd.Timedelta(minutes=1)
    state["risk"]=True
    result=eng.step()
    branch=result["analysis"]["sell_impact"]
    assert branch["cohort"]=="11:00" and branch["anchor"]=="11:20"
    for compact in (False,True):
        s=render_report(result,state["end"],compact=compact)
        assert "卖出能少亏多少" in s and "每笔卖单固定 5 美元" in s and "卖 800 美元" in s
        assert "0.50%" in s and "金额减半" not in s
        assert "卖出能少亏多少" not in render_report(result,state["end"]+pd.Timedelta(minutes=4),compact=compact)
    assert eng.step()["analysis"]["sell_impact"] == branch
    state["end"]+=pd.Timedelta(minutes=1)
    assert eng.step()["analysis"]["sell_impact"]["anchor"]=="11:20"
    state["end"]+=pd.Timedelta(minutes=3)
    unknown=eng.step()["analysis"]["sell_impact"]
    assert unknown["cohort"]=="all" and unknown["anchor"] is None
    state["end"]+=pd.Timedelta(minutes=1)
    state["skew"]=-1
    assert eng.step()["analysis"]["sell_impact"]["status"]=="UNAVAILABLE"


def test_history_bounds_and_partial_session_and_other_symbols():
    for dt in ("2026-11-20T16:20:00Z","2026-11-27T16:20:00Z"):
        eng,state=market(start=dt)
        state["risk"]=True
        assert eng.step()["analysis"]["sell_impact"]["status"]=="UNAVAILABLE"
    eng,state=market(ticker="QQQ")
    state["risk"]=True
    assert "sell_impact" not in eng.step()["analysis"]
    eng,state=market(review=lambda p:{**opinion(p),"action":"WAIT"})
    state["risk"]=True
    assert "sell_impact" not in eng.step()["analysis"]


def test_close_horizon_and_missing_asset(monkeypatch):
    eng,state=market(start="2026-11-23T20:58:00Z")
    eng.step()
    state.update(end=state["end"]+pd.Timedelta(minutes=1),risk=True)
    result=eng.step()
    assert [r["horizon"] for r in result["analysis"]["sell_impact"]["rows"]]==["收盘"]
    def missing():
        raise FileNotFoundError
    monkeypatch.setattr("tradingagents.intraday_sell._reference",missing)
    result=eng.step()
    assert result["status"]=="READY" and result["analysis"]["sell_impact"]["status"]=="UNAVAILABLE"


def test_saved_percentage_fee_keeps_its_original_label():
    branch = dict(status="HISTORICAL", anchor=None, unit_usd=1000, cost_bps=5,
                  sample_start="2026-11-19", sample_end="2026-11-20", sessions=2,
                  rows=[dict(horizon="60",n=2,days=2,down_saved=8.0,
                             rebound_missed=3.0,average_net=2.5,profitable=1)])
    text = "\n".join(sell_lines(branch, False))
    assert "存档采用旧卖出费率 0.05%" in text and "+2.50 美元" in text
    assert "每笔卖单固定" not in text
