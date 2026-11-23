from dataclasses import replace

import pandas as pd
import pytest

from tests.test_intraday import opinion, snapshot
from tradingagents import extended_context as ext
from tradingagents.intraday import IntradayEngine, render_report


def history(day="2026-11-20"):
    start, end = ext.windows(day)["premarket"]
    grid = pd.date_range(start, end, freq="5min", inclusive="left")
    price = pd.Series([100-i/100 for i in range(len(grid))], index=grid)
    frame = pd.DataFrame({name:price for name in ext.FIELDS})
    return start, end, {"TQQQ":frame, "QQQ":frame.copy()}


def observation(day):
    start, end, frames = history(day)
    return {"date":day, "premarket":ext.session_reading(frames,start,end,"Yahoo")}


def test_windows_use_ny_calendar_and_only_completed_synchronous_premarket_closes():
    assert ext.windows("2026-11-02")["overnight"][0].isoformat()=="2026-11-01T20:00:00-05:00"
    assert ext.windows("2026-03-09")["overnight"][0].isoformat()=="2026-03-08T20:00:00-04:00"
    start, end, frames = history()
    base = ext.session_reading(frames,start,end,"Yahoo")
    assert base["direction"]=="WEAK" and base["common_bars"]==66
    assert base["return_pct"]["TQQQ"] == pytest.approx((99.36/99.99-1)*100)
    for f in frames.values():
        f.loc[start-pd.Timedelta(hours=2)] = 200
        f.loc[end] = 500
    assert ext.session_reading(frames,start,end,"Yahoo") == base
    frames["QQQ"]=frames["QQQ"].drop([start,start+pd.Timedelta(minutes=5)])
    assert ext.session_reading(frames,start,end,"Yahoo")["status"]=="UNAVAILABLE"


def test_wrong_identity_is_rejected_and_no_regular_bars_enter(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"chart":{"result":[{"meta":{"symbol":"SPY","currency":"USD","exchangeTimezoneName":ext.NY_TZ,"instrumentType":"ETF"}}]}}
    monkeypatch.setattr(ext.yahoo_http,"get",lambda *a,**kw:Response())
    with pytest.raises(ValueError):
        ext.yahoo_bars("TQQQ",*ext.windows("2026-11-20")["premarket"])


def test_premarket_reaches_sell_reviewer_and_report_only_during_regular_risk(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_SELL_CALIBRATION", str(tmp_path / "missing.json"))
    clock=[pd.Timestamp("2026-11-23T15:05:30Z")]
    bearish=[False]
    seen, calls = [], []
    def fetch(t):
        side = -1 if bearish[0] else 1
        end = clock[0].floor("min")
        return replace(snapshot(t), bar_start=(end-pd.Timedelta(minutes=1)).isoformat(),bar_end=end.isoformat(),
                       price=100+side,vwap=100,ema9=100+side,ema20=100,momentum5_pct=side)
    def pre(day):
        calls.append(day)
        return observation(day)
    def review(packet):
        seen.append(packet)
        return opinion(packet)
    engine=IntradayEngine("TQQQ",{},fetch=fetch,clock=lambda:clock[0],review=review,premarket_fetch=pre)
    assert engine.step()["action"]=="WATCH_LONG" and not calls and "premarket_context" not in seen[-1]
    bearish[0]=True
    clock[0]+=pd.Timedelta(minutes=1)
    result=engine.step()
    assert result["action"]=="WATCH_RISK" and seen[-1]["premarket_context"]==result["premarket_context"]
    assert result["premarket_context"]["premarket"]["direction"]=="WEAK"
    for compact in (True,False):
        text=render_report(result,clock[0],compact=compact)
        assert "两者同跌" in text and "历史卖出校准数据暂时不可用" in text
        assert "开盘前的卖出背景" not in render_report(result,clock[0]+pd.Timedelta(minutes=4),compact=compact)
    engine.step()
    assert calls==["2026-11-23"]
    clock[0]=pd.Timestamp("2026-11-24T13:00Z")
    assert engine.step()["action"]=="WAIT" and len(calls)==1
    clock[0]=pd.Timestamp("2026-11-24T15:00Z")
    engine.step()
    assert calls==["2026-11-23","2026-11-24"]


def test_missing_or_wrong_day_does_not_change_regular_signal():
    calls, packets = [], []
    stamp=pd.Timestamp("2026-11-23T15:06Z")
    def pre(day):
        calls.append(day)
        if len(calls)==1:
            raise TimeoutError
        return observation("2026-11-20")
    def fetch(t):
        return replace(snapshot(t),bar_end=stamp.isoformat(),price=99,vwap=100,ema9=99,ema20=100,momentum5_pct=-1)
    def review(packet):
        packets.append(packet)
        return opinion(packet)
    engine=IntradayEngine("TQQQ",{},fetch=fetch,clock=lambda:stamp,review=review,premarket_fetch=pre)
    result=engine.step()
    assert result["action"]=="WATCH_RISK" and result["premarket_context"]["premarket"]["status"]=="UNAVAILABLE"
    assert "premarket_context" not in packets[-1]
    engine.step()
    assert len(calls)==1
    stamp+=pd.Timedelta(minutes=16)
    assert engine.step()["premarket_context"]["premarket"]["status"]=="UNAVAILABLE" and len(calls)==2
