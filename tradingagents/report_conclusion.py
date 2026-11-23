"""Project the verified daily snapshot and allocation checks into one public decision."""
from datetime import date

from langchain_core.messages import ToolMessage


def market_coverage(messages, ticker, trade_date):
    """Read only the first-party snapshot tool's fixed provenance header.

    This works with existing recorded snapshots and does not fetch prices again.
    An analyst's prose, proxy ticker or snapshot requested for an earlier date
    cannot establish coverage of the execution instrument for this analysis.
    """
    unknown = {"status": "UNVERIFIED", "ticker": ticker, "as_of": trade_date}
    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage) or msg.name != "get_verified_market_snapshot":
            continue
        if not isinstance(msg.content, str) or getattr(msg, "status", "") == "error":
            return unknown
        header, separator, _ = msg.content.partition("\n### Latest verified OHLCV row")
        if not separator:
            return unknown
        lines = header.splitlines()
        if not lines or lines[0] != f"## Verified market data snapshot for {ticker.upper()}":
            return unknown
        fields = dict(line[2:].split(": ", 1) for line in lines if line.startswith("- ") and ": " in line)
        if fields.get("Requested analysis date") != trade_date:
            continue
        latest = fields.get("Latest trading row used")
        expected = fields.get("Last completed scheduled US session")
        try:
            last_day = date.fromisoformat(latest)
            as_of = date.fromisoformat(trade_date)
            session_day = date.fromisoformat(expected) if expected else None
        except (ValueError, TypeError):
            return unknown
        if last_day > as_of or (session_day and session_day > as_of):
            return unknown
        result = {**unknown, "latest": latest, "expected": expected}
        if not fields.get("Data source") or fields.get("Latest data row") != latest:
            return result
        if ("HISTORICAL REFERENCE ONLY" in fields or "- WARNING: data does not yet cover" in header
                or (session_day and last_day < session_day)):
            result["status"] = "STALE"
        elif (session_day and last_day > session_day) or "- WARNING: latest row is provisional" in header:
            result["status"] = "PROVISIONAL"
        elif session_day == last_day:
            result["status"] = "COVERED"
        elif session_day is None:
            result["status"] = "DATED_ONLY"
        return result
    return unknown


def project_conclusion(state, validation):
    """No free-form PM sizing text is a source of reportable quantities."""
    from tradingagents.agents.rating import RATINGS_5_TIER, RATING_REVIEW, run_rating

    coverage = state.get("market_coverage") or {}
    status = coverage.get("status")
    if coverage.get("ticker") != state.get("company_of_interest") or coverage.get("as_of") != state.get("trade_date"):
        status = "UNVERIFIED"
    covered = status == "COVERED" and bool(coverage.get("latest")) and coverage.get("latest") == coverage.get("expected")
    rating = run_rating(state)
    research_rating = rating if rating in RATINGS_5_TIER else RATING_REVIEW
    checked = validation.get("validated_allocation")
    passed = validation["status"] in {"CHECKS_PASSED", "NO_CHANGE"} and checked is not None
    if not covered:
        latest, expected = coverage.get("latest"), coverage.get("expected")
        if status == "STALE" and latest and expected:
            reason = f"行情仅截至 {latest}，缺少最近已完成交易日 {expected} 的数据。"
        elif status == "PROVISIONAL" and latest:
            reason = f"{latest} 的行情尚未确认为收盘数据。"
        elif status == "DATED_ONLY":
            reason = f"已取得 {latest} 的日线，此市场的最近完整交易日尚未核验。"
        else:
            reason = "未取得本标的在分析日可核实的最近完整交易日行情。"
        validation = {**validation,
                      "status": validation["status"] if validation["status"] in {"BLOCKED", "UNSUPPORTED"} else "NEEDS_INPUT",
                      "reasons": [reason, *validation.get("reasons", [])],
                      "checks": [], "calculations": {}, "validated_allocation": None}
        label = "等待行情核验"
        detail = reason + " 补齐并核实行情后重新分析。当前没有通过校验的仓位数量。"
        rating = RATING_REVIEW
    else:
        rating = research_rating
        basis = f"日线已覆盖最近已完成交易日 {coverage['expected']}。"
        if passed:
            label = "仓位无需调整" if validation["status"] == "NO_CHANGE" else "仓位已通过配置校验"
            detail = (f"{basis} 研究评级为 {rating}。按本次账户快照和已配置规则，"
                      f"目标 {checked['target_quantity']:g} 股，变动 {checked['delta_quantity']:+g} 股。")
        else:
            label = {"BLOCKED": "仓位校验未通过", "UNSUPPORTED": "仓位校验暂不支持"}.get(validation["status"], "仅供研究，仓位待核验")
            if any(r.startswith("Configure explicit risk limits:") for r in validation.get("reasons", [])):
                next_step = "请先补全风险参数，再重新校验。"
            else:
                next_step = "请按仓位校验中列出的原因复核。"
            detail = f"{basis} 研究评级为 {rating}。当前没有通过校验的仓位数量。{next_step}"
    summary = (f"**{label}**\n\n{detail}\n\n"
               "后续章节保留各分析师的研究意见。涉及仓位时，以本摘要和仓位校验结果为准。")
    return {"report_status": label, "decision_summary": summary, "final_rating": rating,
            "research_rating": research_rating,
            "risk_validation": validation, "validated_allocation": validation.get("validated_allocation")}
