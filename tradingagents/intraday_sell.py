"""Historical cash-versus-hold comparison for confirmed TQQQ intraday risk watches."""
import json
import os
from pathlib import Path

from tradingagents.dataflows.intraday import aware_utc
from tradingagents.dataflows.vendors.yahoo.sessions import session_close
from tradingagents.intraday_analysis import technical_action


def _reference():
    configured = os.environ.get("TRADINGAGENTS_SELL_CALIBRATION")
    data = Path(configured).expanduser() if configured else Path.home() / ".tradingagents" / "tqqq_sell_impact.json"
    return json.loads(data.read_text(encoding="utf-8"))


def episode_start(snaps, previous, rule, anchor):
    """Only an aligned consecutive pair can establish or carry forward an onset."""
    if rule != "WATCH_RISK" or [s.ticker for s in snaps] != ["TQQQ", "QQQ"]:
        return None
    end = aware_utc(snaps[0].bar_end)
    if end != aware_utc(snaps[1].bar_end):
        return None
    if all(s.bars == 21 for s in snaps):
        return end
    if not all(s.ticker in previous for s in snaps):
        return None
    prior = [previous[s.ticker] for s in snaps]
    gaps = [(end-aware_utc(s.bar_end)).total_seconds() for s in prior]
    if gaps not in ([0,0],[60,60]):
        return None
    if all(technical_action(s)=="WATCH_RISK" for s in prior):
        return anchor
    return end if gaps == [60,60] else None


def sell_impact(snaps, anchor):
    end = aware_utc(snaps[0].bar_end)
    if end != aware_utc(snaps[1].bar_end):
        return {"status":"UNAVAILABLE","reason":"两只 ETF 的分钟结束时间不同，等待同步。"}
    ny = end.tz_convert("America/New_York")
    day = ny.tz_localize(None).normalize()
    close = session_close(day)
    try:
        ref = _reference()
        if ref["version"] != 2 or ref["ticker"] != "TQQQ":
            raise ValueError("unexpected calibration")
        if str(ny.date()) <= ref["end"]:
            return {"status":"UNAVAILABLE","reason":"当前回放日期早于或处于历史校准期，避免使用未来行情。"}
        opened = day.tz_localize("America/New_York").replace(hour=9,minute=30)
        if (close-opened).total_seconds()/60 != ref["session_minutes"]:
            return {"status":"UNAVAILABLE","reason":"本次校准只有完整交易日，提前收盘日暂无可比样本。"}
        origin = anchor if anchor is not None else end
        local = origin.tz_convert("America/New_York")
        bucket = local.replace(minute=(local.minute//30)*30).strftime("%H:%M") if anchor is not None else "all"
        stats = ref["cohorts"].get(bucket,{})
        remaining = (close-origin).total_seconds()/60
        elapsed = (end-origin).total_seconds()/60
        rows = []
        for h in ("5","15","30","60","收盘"):
            if h not in stats or (h!="收盘" and not elapsed<int(h)<=remaining):
                continue
            rows.append({"horizon":h, **stats[h]})
        if not rows:
            return {"status":"UNAVAILABLE","reason":"这个时段没有可比较的历史观察样本。"}
        return dict(status="HISTORICAL", cohort=bucket, anchor=local.strftime("%H:%M") if anchor is not None else None,
                    sample_start=ref["start"],sample_end=ref["end"],sessions=ref["sessions"],
                    sale_fee_usd=ref["sale_fee_usd"],unit_usd=ref["unit_usd"],rows=rows)
    except (OSError, ValueError, KeyError, TypeError):
        return {"status":"UNAVAILABLE","reason":"历史卖出校准数据暂时不可用。"}


def sell_lines(branch, compact):
    lines = ["## 卖出能少亏多少", ""]
    if branch["status"] != "HISTORICAL":
        return lines + [branch["reason"], ""]
    if branch["anchor"]:
        cohort = (f"本段技术风险从美东 {branch['anchor']} 开始，参照历史同一半小时内的新触发。"
                  "持续提示沿用首次触发时点。")
    else:
        cohort = "本段风险的起点未知。以下为全时段历史新触发的参考，当前这一分钟的避损金额未知。"
    fee = branch.get("sale_fee_usd")
    unit = branch["unit_usd"]
    fee_note = (f"每笔卖单固定 {fee:g} 美元，卖出后下跌超过 {fee/unit:.2%} 才比持有划算。"
                if fee is not None else f"存档采用旧卖出费率 {branch['cost_bps']/100:.2f}%。")
    lines += [cohort, f"以一笔卖出 {unit:,} 美元 TQQQ 并持现金为例。{fee_note}",
              f"技术触发样本期 {branch['sample_start']} 至 {branch['sample_end']}（{branch['sessions']} 个交易日）。",""]
    rows = branch["rows"]
    if compact:
        fixed = [r for r in rows if r["horizon"]!="收盘"]
        rows = fixed[-1:] + [r for r in rows if r["horizon"]=="收盘"]
    lines += ["| 观察至 | 样本 | 下跌时卖出净差 | 反弹时平均少赚 | 全样本平均净差 | 卖出占优 |",
              "|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        h = row["horizon"]
        label = h if h=="收盘" else h+" 分钟"
        saved = "—" if row["down_saved"] is None else f"{row['down_saved']:+.2f} 美元"
        missed = "—" if row["rebound_missed"] is None else f"${row['rebound_missed']:.2f}"
        lines.append(f"| {label} | {row['n']} 次 / {row['days']} 天 | {saved} | {missed} | {row['average_net']:+.2f} 美元 | {row['profitable']/row['n']:.1%} |")
    lines += ["","净差为正表示卖出少亏或多赚，负值表示继续持有占优。"]
    if fee is not None:
        close = next((r for r in rows if r["horizon"]=="收盘"), None)
        if close:
            examples = [f"卖 {a} 美元为 {s['average_net']:+.2f} 美元" for a,s in close["sizes"].items()]
            lines += ["","到收盘的平均净差（各卖一单）：" + "，".join(examples) + "。"]
    return lines + [""]
