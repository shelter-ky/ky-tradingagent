"""Explain the minute rule and compare successive observations of the same session."""
from tradingagents.dataflows.intraday import aware_utc


FACTOR_NAMES = {"price_vwap": "价格相对 VWAP", "ema_spread": "EMA9 相对 EMA20", "momentum5": "5 分钟动量"}
SIDE_NAMES = {1: "偏强", 0: "持平", -1: "偏弱"}
RULE_NAMES = {"WATCH_LONG": "三项偏强", "WATCH_RISK": "三项偏弱", "WAIT": "未形成一致方向"}
OBSERVATIONS = {"WATCH_LONG": "买入条件观察", "WATCH_RISK": "风险观察", "WAIT": "等待"}
REQUIREMENTS = {
    1: {"price_vwap": "收盘高于 VWAP", "ema_spread": "EMA9 高于 EMA20", "momentum5": "5 分钟动量为正"},
    -1: {"price_vwap": "收盘低于 VWAP", "ema_spread": "EMA9 低于 EMA20", "momentum5": "5 分钟动量为负"},
}


def side(left, right):
    if left > right:
        return 1
    if left < right:
        return -1
    return 0


def factor_sides(snap):
    return {
        "price_vwap": side(snap.price, snap.vwap),
        "ema_spread": side(snap.ema9, snap.ema20),
        "momentum5": side(snap.momentum5_pct, 0),
    }


def rule_for(sides):
    if all(value == 1 for value in sides.values()):
        return "WATCH_LONG"
    if all(value == -1 for value in sides.values()):
        return "WATCH_RISK"
    return "WAIT"


def technical_action(snap):
    return rule_for(factor_sides(snap))


def read_symbol(snap):
    sides = factor_sides(snap)
    return {
        "ticker": snap.ticker, "bar_end": snap.bar_end, "price": snap.price,
        "sides": sides, "rule_action": rule_for(sides),
        "values_pct": {
            "price_vwap": (snap.price - snap.vwap) / snap.vwap * 100,
            "ema_spread": (snap.ema9 - snap.ema20) / snap.ema20 * 100,
            "momentum5": snap.momentum5_pct,
        },
        "from_session_open_pct": (snap.price - snap.session_open) / snap.session_open * 100,
    }


def compare_symbol(current, previous):
    if previous is None:
        return {"kind": "FIRST"}
    now = aware_utc(current["bar_end"])
    then = aware_utc(previous["bar_end"])
    if now.tz_convert("America/New_York").date() != then.tz_convert("America/New_York").date():
        return {"kind": "FIRST"}
    if now == then and current == previous:
        return {"kind": "UNCHANGED", "previous_bar_end": previous["bar_end"]}
    changes = {key: [previous["sides"][key], value] for key, value in current["sides"].items()
               if value != previous["sides"][key]}
    return {
        "kind": "REVISED" if now == then else "NEW",
        "previous_bar_end": previous["bar_end"],
        "elapsed_minutes": int((now - then).total_seconds() // 60),
        "price_change_pct": (current["price"] - previous["price"]) / previous["price"] * 100,
        "momentum_change_pp": current["values_pct"]["momentum5"] - previous["values_pct"]["momentum5"],
        "side_changes": changes,
    }


def describe_alignment(readings):
    directions = "，".join(f"{r['ticker']} {RULE_NAMES[r['rule_action']]}" for r in readings)
    if len(readings) == 1:
        return directions + "。"
    rules = {r["rule_action"] for r in readings}
    if len(rules) == 1 and "WAIT" not in rules:
        return directions + "，两者同向。"
    return directions + "，未满足双标的同向条件。"


def format_change(change):
    kind = change["kind"]
    if kind == "FIRST":
        return "本次建立比较基准。"
    if kind == "UNCHANGED":
        return "还是同一根分钟线，本页指标未变。"
    label = "同一分钟的数据修订" if kind == "REVISED" else f"距上次行情 {change['elapsed_minutes']} 分钟"
    moves = [f"{FACTOR_NAMES[k]}从{SIDE_NAMES[a]}转为{SIDE_NAMES[b]}"
             for k, (a, b) in change["side_changes"].items()]
    return (f"{label}，收盘价变化 {change['price_change_pct']:+.3f}%，"
            f"5 分钟动量变化 {change['momentum_change_pp']:+.3f} 个百分点。"
            + ("，".join(moves) + "。" if moves else "三项指标方向均未变。"))


def analysis_lines(analysis, *, action, rule, compact):
    lines = ["## 盘中分析", "", analysis["alignment"], ""]
    for symbol in analysis["symbols"]:
        ticker = symbol["ticker"]
        values = symbol["values_pct"]
        if compact:
            lines.append(f"- **{ticker}**　偏离 VWAP {values['price_vwap']:+.3f}%，"
                         f"均线差 {values['ema_spread']:+.3f}%，动量 {values['momentum5']:+.3f}%。")
        else:
            factors = [f"{name} {values[key]:+.3f}%（{SIDE_NAMES[symbol['sides'][key]]}）"
                       for key, name in FACTOR_NAMES.items()]
            lines.append(f"- **{ticker}**　" + "，".join(factors) + f"。相对本日开盘 {symbol['from_session_open_pct']:+.3f}%。")
    if rule == "WAIT" and not compact:
        lines.append("")
        for side_needed, requirements in REQUIREMENTS.items():
            missing = []
            for symbol in analysis["symbols"]:
                conditions = [requirements[key] for key, value in symbol["sides"].items() if value != side_needed]
                if conditions:
                    missing.append(symbol["ticker"] + " " + "、".join(conditions))
            lines.append(f"- 要形成一致{SIDE_NAMES[side_needed]}，仍需 " + "，以及 ".join(missing) + "。")
    elif rule != "WAIT" and action == "WAIT":
        lines.extend(["", "技术条件已同向，模型复核后本轮仍为等待。"])
    lines += ["", "**与上一轮相比**", ""]
    previous = analysis.get("previous_action")
    if previous is not None:
        if previous == action:
            lines.append(f"两轮均为{OBSERVATIONS[action]}。")
        else:
            lines.append(f"观察结果由{OBSERVATIONS[previous]}变为{OBSERVATIONS[action]}。")
        lines.append("")
    for ticker, change in analysis["changes"].items():
        lines.append(f"- {ticker}　{format_change(change)}")
    return lines + [""]
