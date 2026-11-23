"""Completed paired premarket observations used only in regular-session sell research."""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pandas as pd
from curl_cffi import requests as yahoo_http

from tradingagents.dataflows.vendors.yahoo.sessions import NY_TZ, is_session


SYMBOLS = ("TQQQ", "QQQ")
FIELDS = ("open", "high", "low", "close")


def windows(day):
    date = pd.Timestamp(day).date()
    def local(d, hhmm):
        return pd.Timestamp(f"{d} {hhmm}", tz=NY_TZ)
    pre = local(date, "04:00")
    return {"overnight": (local(date-timedelta(days=1), "20:00"), pre),
            "premarket": (pre, local(date, "09:30"))}


def yahoo_bars(symbol, start, end):
    response = yahoo_http.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"interval":"5m", "period1":int(start.timestamp()), "period2":int(end.timestamp()),
                "includePrePost":"true"}, impersonate="chrome", timeout=8)
    response.raise_for_status()
    data = response.json()["chart"]["result"][0]
    meta = data["meta"]
    if (meta.get("symbol"),meta.get("currency"),meta.get("exchangeTimezoneName"),meta.get("instrumentType")) != (symbol,"USD",NY_TZ,"ETF"):
        raise ValueError("unexpected market identity")
    return pd.DataFrame(data["indicators"]["quote"][0],
                        index=pd.to_datetime(data["timestamp"],unit="s",utc=True))


def unavailable(source, reason):
    return {"status":"UNAVAILABLE","source":source,"reason":reason}


def session_reading(frames, start, end, source):
    """Compare common, observed five-minute closes near both session boundaries."""
    grid = pd.date_range(start,end,freq="5min",inclusive="left").tz_convert("UTC")
    closes = {}
    for symbol in SYMBOLS:
        f = frames.get(symbol)
        if f is None or f.empty or not isinstance(f.index,pd.DatetimeIndex) or f.index.tz is None:
            return unavailable(source,"缺少带时区的双标的行情")
        f = f.loc[(f.index>=start) & (f.index<end)]
        if f.index.has_duplicates or not set(FIELDS).issubset(f.columns) or not f.index.isin(grid).all():
            return unavailable(source,"分钟线字段或时间边界异常")
        f = f.loc[:,list(FIELDS)].apply(pd.to_numeric,errors="coerce")
        valid = (f.gt(0).all(axis=1) & f.lt(float("inf")).all(axis=1)
                 & f.high.ge(f.max(axis=1)) & f.low.le(f.min(axis=1)))
        closes[symbol] = f.loc[valid,"close"]
    common = pd.DataFrame(closes).reindex(grid).dropna()
    first = common.loc[common.index<grid[0]+pd.Timedelta(minutes=15)]
    last = common.loc[common.index>=grid[-1]-pd.Timedelta(minutes=10)]
    if len(first)<2 or len(last)<2 or len(common)<math.ceil(len(grid)/2):
        return unavailable(source,f"有效同步五分钟线 {len(common)}/{len(grid)}，覆盖或首尾报价不足")
    changes = (last.median()/first.median()-1)*100
    direction = "WEAK" if changes.lt(0).all() else "STRONG" if changes.gt(0).all() else "MIXED"
    return {"status":"AVAILABLE","source":source,"direction":direction,"return_pct":changes.to_dict(),
            "common_bars":len(common),"expected_bars":len(grid),
            "first_bar":first.index[0].isoformat(),"last_bar_end":(last.index[-1]+pd.Timedelta(minutes=5)).isoformat()}


def fetch_context(day):
    spans = windows(day)
    result = {"date":str(pd.Timestamp(day).date())}
    if not is_session(pd.Timestamp(day)) or pd.Timestamp.now(tz=NY_TZ)<spans["premarket"][1]:
        raise ValueError("extended sessions must already be complete")
    start,end = spans["premarket"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        pre = {s:pool.submit(yahoo_bars,s,start,end) for s in SYMBOLS}
        try:
            result["premarket"] = session_reading({s:f.result() for s,f in pre.items()},start,end,"Yahoo")
        except (ValueError, KeyError, IndexError, TypeError, OSError, yahoo_http.RequestsError):
            result["premarket"] = unavailable("Yahoo","盘前行情获取失败")
    return result


def context_lines(context):
    lines = ["## 开盘前的卖出背景",""]
    labels = {"WEAK":"两者同跌，与盘中弱势同向","STRONG":"两者同涨，盘中已转弱","MIXED":"两者方向未一致"}
    row = context["premarket"]
    if row["status"] == "AVAILABLE":
        returns = row["return_pct"]
        lines.append(f"盘前：TQQQ {returns['TQQQ']:+.2f}%，QQQ {returns['QQQ']:+.2f}%。"
                     f"{labels[row['direction']]}（{row['source']}，{row['common_bars']}/{row['expected_bars']} 根同步五分钟线）。")
        lines.append("统计美东 04:00 至 09:30，比较首尾各十五分钟的有效收盘价中位数。")
    else:
        lines.append(f"盘前：{row['reason']}，本轮仅据常规盘行情判断。")
    return lines+["跨午夜夜盘暂无已核验行情。",""]
