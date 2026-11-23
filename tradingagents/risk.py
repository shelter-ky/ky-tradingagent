"""Deterministic single-instrument allocation checks, independent of model prose.

No default risk appetite, market-price extraction, order execution or portfolio
VaR is implied. The caller supplies the book, dated unadjusted price and limits.
The model proposes a final weight; only this module derives quantity and costs.
"""
from __future__ import annotations

import json
import math
from datetime import date
from decimal import Decimal, ROUND_FLOOR, localcontext
from numbers import Real
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.sessions import is_us_equity
from tradingagents.instruments import profile_from_state
from tradingagents.portfolio import PortfolioContext
from tradingagents.report_conclusion import project_conclusion

VERSION = "allocation-risk-v1"


def _number_only(value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, Real)):
        raise ValueError("Must be a finite numeric value, not text or bool")
    return value


class AllocationProposal(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    target_weight: float = Field(ge=0, le=1, description="Final instrument market-value weight of net assets as a fraction, e.g. 0.05, never 5 for 5%. Not an increment.")
    stop_loss: float | None = Field(default=None, gt=0, description="Absolute stop-price scenario in the execution instrument's quote currency; not a guaranteed loss cap. Required for a nonzero target.")

    _numbers = field_validator("target_weight", "stop_loss", mode="before")(_number_only)


class RiskLimits(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    max_position_weight: float | None = Field(default=None, ge=0, le=1)
    max_daily_exposure_weight: float | None = Field(default=None, ge=0)
    max_stop_scenario_loss_fraction: float | None = Field(default=None, ge=0, le=1)
    min_cash_weight: float | None = Field(default=None, ge=0, le=1)
    slippage_bps: float | None = Field(default=None, ge=0, lt=10000)
    fee_per_order: float | None = Field(default=None, ge=0)
    quantity_step: float = Field(default=1, gt=0, description="Share increment; default whole shares. No contract multipliers.")

    _numbers = field_validator("*", mode="before")(_number_only)


def load_risk_limits(path):
    try:
        return RiskLimits.model_validate(json.loads(Path(path).read_text(encoding="utf-8-sig"))).model_dump()
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"Risk limits file is not usable ({type(exc).__name__}); check JSON fields and finite numeric values.") from None


def _D(value):
    return Decimal(str(value))


def _result(status, reasons, *, calculations=None, checks=None, allocation=None, **context):
    return {"version": VERSION, "status": status, "reasons": reasons,
            "basis": "caller_supplied_snapshot; not independently market-verified",
            "scope": "single US equity/ETF, long-only, per-share reference; no aggregate portfolio risk",
            "calculations": calculations or {}, "checks": checks or [],
            "validated_allocation": allocation, **context}


def validate_allocation(state, raw_limits):
    """No parsing of a prose sizing suggestion can produce a passed allocation."""
    try:
        policy = RiskLimits.model_validate({} if raw_limits is None else raw_limits)
    except (ValidationError, TypeError):
        return _result("BLOCKED", ["Risk limits are invalid; use finite numbers and supported policy fields."])
    missing = [name for name, value in policy.model_dump().items() if value is None]
    if missing:
        return _result("NEEDS_INPUT", ["Configure explicit risk limits: " + ", ".join(missing)], policy=policy.model_dump())
    if state.get("portfolio_data") is None:
        return _result("NEEDS_INPUT", ["Account holdings/cash/net asset value snapshot was not supplied."], policy=policy.model_dump())
    try:
        book = PortfolioContext.model_validate(state["portfolio_data"])
        trade_date = date.fromisoformat(str(state["trade_date"]))
        profile = profile_from_state(state)
    except (ValidationError, ValueError, TypeError, KeyError):
        return _result("BLOCKED", ["Portfolio, instrument profile or analysis date is invalid."])
    symbol = normalize_symbol(state["company_of_interest"])
    if profile.kind not in {"stock", "etf"} or not is_us_equity(symbol):
        return _result("UNSUPPORTED", ["This validator supports US equity/ETF shares only; FX, futures, crypto and non-US instruments require their own valuation rules."])
    leverage = profile.daily_leverage if profile.kind == "etf" else 1
    if leverage is None:
        return _result("NEEDS_INPUT", ["ETF daily exposure multiplier is unverified; provide an explicit instrument profile."])
    if leverage <= 0:
        return _result("UNSUPPORTED", ["Inverse ETF exposure is outside this long-direction validator."])
    missing = []
    if book.net_asset_value is None:
        missing.append("net_asset_value (never substituted with average entry cost)")
    if book.cash is None:
        missing.append("cash")
    if not book.currency:
        missing.append("currency")
    if book.as_of_date != trade_date:
        missing.append("portfolio as_of_date must match the analysis date")
    quotes = [q for q in book.reference_quotes if normalize_symbol(q.ticker) == symbol]
    if len(quotes) != 1:
        missing.append("exactly one reference quote for the execution instrument")
    if missing:
        return _result("NEEDS_INPUT", missing, policy=policy.model_dump())
    quote = quotes[0]
    if quote.as_of_date != trade_date:
        return _result("NEEDS_INPUT", ["Reference quote must be recorded on the analysis date; stale/future prices are not substituted."])
    if quote.price_basis != "unadjusted":
        return _result("BLOCKED", ["Reference must be an explicit unadjusted per-share price; adjusted total-return prices cannot size shares."])
    if book.currency.strip().upper() != "USD" or quote.currency.strip().upper() != "USD":
        return _result("UNSUPPORTED", ["Account and instrument must both be USD; automatic FX conversion is not implemented."])
    held = book.positions_in(symbol)
    if any(p.quantity < 0 for p in book.positions) or book.cash < 0:
        return _result("UNSUPPORTED", ["Short holdings or negative free cash need margin/borrow rules; this validator is long-only."])
    if state.get("allocation_proposal") is None:
        return _result("NEEDS_INPUT", ["No typed allocation proposal was provided. Free-text quantities are unvalidated research only."])
    try:
        proposal = AllocationProposal.model_validate(state["allocation_proposal"])
    except (ValidationError, TypeError):
        return _result("BLOCKED", ["Allocation proposal has invalid weight/stop fields; use a fraction and a positive finite absolute stop."])
    try:
        with localcontext() as context:
            context.prec = 50
            return _calculate(state, policy, book, quote, proposal, leverage, held)
    except (ArithmeticError, ValueError, OverflowError):
        return _result("BLOCKED", ["Allocation arithmetic is not representable; no quantities were validated."])


def _calculate(state, policy, book, quote, proposal, leverage, held):
    from tradingagents.agents.rating import run_rating
    nav, cash, price = map(_D, (book.net_asset_value, book.cash, quote.price))
    step, slip, fee = _D(policy.quantity_step), _D(policy.slippage_bps) / 10000, _D(policy.fee_per_order)
    current = sum((_D(p.quantity) for p in held), Decimal(0))
    if cash + current * price > nav:
        return _result("BLOCKED", ["Cash plus this long position's reference market value exceeds reported NAV; reconcile the snapshot or unsupported liabilities first."])
    if current % step != 0:
        return _result("BLOCKED", ["Current holdings do not match quantity_step; configure the supported share increment."])
    target = ((_D(proposal.target_weight) * nav / price) / step).to_integral_value(rounding=ROUND_FLOOR) * step
    delta = target - current
    rating = run_rating(state)
    action = "Buy" if delta > 0 else "Sell" if delta < 0 else "Hold"
    checks = []

    def check(name, passed, actual, limit):
        checks.append({"name": name, "passed": bool(passed), "actual": actual, "limit": limit})

    direction = ((rating in {"Buy", "Overweight"} and delta >= 0)
                 or (rating == "Hold" and delta == 0)
                 or (rating == "Underweight" and delta <= 0)
                 or (rating == "Sell" and target == 0))
    check("research_direction", direction, action, rating)
    if target > 0 and (proposal.stop_loss is None or not 0 < proposal.stop_loss < quote.price):
        return _result("BLOCKED" if proposal.stop_loss is not None else "NEEDS_INPUT",
                       ["A nonzero long allocation requires a stop scenario strictly between zero and the reference price."], checks=checks)
    transaction_cost = abs(delta) * price * slip + (fee if delta else 0)
    cash_after = cash - delta * price - transaction_cost
    nav_after = nav - transaction_cost
    if nav_after <= 0:
        return _result("BLOCKED", ["Estimated transaction costs consume net assets."], checks=checks)
    target_value = target * price
    weight = target_value / nav_after
    daily_exposure = weight * _D(leverage)
    stop = _D(proposal.stop_loss) if target > 0 else None
    stop_loss = target * (price - stop * (1 - slip)) + fee if target > 0 else Decimal(0)
    scenario_loss = transaction_cost + stop_loss
    loss_limit = nav * _D(policy.max_stop_scenario_loss_fraction)
    cash_floor = nav_after * _D(policy.min_cash_weight)
    check("nonnegative_cash", cash_after >= 0, float(cash_after), 0)
    check("cash_reserve", cash_after >= cash_floor, float(cash_after), float(cash_floor))
    check("position_weight", weight <= _D(policy.max_position_weight), float(weight), policy.max_position_weight)
    check("daily_exposure_weight", daily_exposure <= _D(policy.max_daily_exposure_weight), float(daily_exposure), policy.max_daily_exposure_weight)
    check("stop_scenario_loss", scenario_loss <= loss_limit, float(scenario_loss), float(loss_limit))
    # A fixed illustrative adverse fill tests gap risk beyond the stop.
    # It is a diagnostic, not another policy threshold or an upper loss bound.
    gap_fill = price * Decimal("0.80")
    stress_loss = transaction_cost + target * (price - gap_fill * (1 - slip)) + (fee if target > 0 else 0)
    calculations = {"currency": "USD", "reference_price": float(price),
                    "quote_date": str(quote.as_of_date), "net_asset_value_before": float(nav),
                    "estimated_nav_after_costs": float(nav_after), "current_quantity": float(current),
                    "proposed_target_weight": proposal.target_weight, "calculated_target_quantity": float(target),
                    "calculated_delta_quantity": float(delta), "estimated_cash_after": float(cash_after),
                    "estimated_transaction_cost": float(transaction_cost), "position_weight_after_costs": float(weight),
                    "daily_exposure_weight": float(daily_exposure), "stop_scenario_loss": float(scenario_loss),
                    "stop_loss": float(stop) if stop is not None else None,
                    "illustrative_20pct_down_fill_loss": float(stress_loss)}
    if any(isinstance(v, float) and not math.isfinite(v) for v in calculations.values()):
        return _result("BLOCKED", ["Allocation arithmetic overflowed; no quantities were validated."])
    failed = [c["name"] for c in checks if not c["passed"]]
    status = "BLOCKED" if failed else "NO_CHANGE" if delta == 0 else "CHECKS_PASSED"
    allocation = None if failed else {"ticker": normalize_symbol(state["company_of_interest"]),
        "action": action, "target_quantity": float(target), "delta_quantity": float(delta),
        "reference_price": float(price), "currency": "USD", "stop_loss": calculations["stop_loss"]}
    return _result(status, ["Failed checks: " + ", ".join(failed)] if failed else [],
                   calculations=calculations, checks=checks, allocation=allocation, policy=policy.model_dump())


def render_risk_validation(result):
    status_name = {"NEEDS_INPUT": "待补信息", "BLOCKED": "未通过", "UNSUPPORTED": "暂不支持",
                   "CHECKS_PASSED": "已通过已配置规则", "NO_CHANGE": "无需调整"}.get(result["status"], "需检查")
    lines = ["## 仓位风险校验 / Allocation validation", f"**状态 / Status**: {status_name} ({result['status']})",
             "仅校验调用方提供的账户和报价快照，不独立证明数据真实，也不代表交易指令。评级仍是研究观点。"]
    lines.extend(f"- {reason}" for reason in result["reasons"])
    allocation = result["validated_allocation"]
    if allocation:
        lines.append(f"校验数量：目标 {allocation['target_quantity']:g} 股；变动 {allocation['delta_quantity']:+g} 股。")
    else:
        lines.append("**没有通过校验的仓位数量。模型文字中的金额、股数与仓位比例均为未验证建议。**")
    if result["calculations"]:
        labels = {"currency": "货币", "reference_price": "每股参考价", "quote_date": "报价日期",
                  "net_asset_value_before": "交易前账户净值", "estimated_nav_after_costs": "扣除估算费用后的净值",
                  "current_quantity": "当前股数", "proposed_target_weight": "提议目标比例（小数）",
                  "calculated_target_quantity": "计算目标股数（不代表通过）", "calculated_delta_quantity": "计算变动股数（不代表通过）",
                  "estimated_cash_after": "估算剩余现金", "estimated_transaction_cost": "估算本次费用及滑点",
                  "position_weight_after_costs": "费用后仓位比例（小数）", "daily_exposure_weight": "单标的每日目标暴露比例（小数）",
                  "stop_scenario_loss": "止损成交情景损失（含估算成本）", "stop_loss": "提议止损价",
                  "illustrative_20pct_down_fill_loss": "下跌 20% 成交示例损失（非最坏情况）"}
        rows = ["| 计算项目 | 数值 |", "|---|---:|"]
        rows.extend(f"| {labels.get(name, name)} | {value} |" for name, value in result["calculations"].items())
        lines.append("\n".join(rows))
    if result["checks"]:
        lines.append("\n".join(["| 校验项 | 结果 | 计算值 | 约束值 |", "|---|---|---|---|"] +
            [f"| {c['name']} | {'PASS' if c['passed'] else 'FAIL'} | {c['actual']} | {c['limit']} |" for c in result["checks"]]))
    lines.append("止损只是价格情景；跳空、流动性和滑点可能造成更大损失。额外列出的下跌 20% 成交情景仅为固定示例，"
                 "不表示最坏情形、发生概率或已接受的风险预算。杠杆暴露是单个标的的每日目标倍数近似，"
                 "不涵盖全组合相关性、多日路径、税费、借券与保证金。")
    return "\n\n".join(lines)


def risk_validation_node(state):
    result = validate_allocation(state, get_config().get("risk_limits", {}))
    return checked_report_fields(state, result)


def checked_report_fields(state, result):
    fields = project_conclusion(state, result)
    report = render_risk_validation(fields["risk_validation"])
    fields["risk_validation_report"] = report
    fields["final_trade_decision"] = fields["decision_summary"]
    return fields


def report_state(state):
    """Re-export an older checked run conservatively when it lacks a coverage record."""
    if state.get("risk_validation") and not state.get("decision_summary"):
        return {**state, **checked_report_fields(state, state["risk_validation"])}
    return state


def policy_prompt():
    try:
        policy = RiskLimits.model_validate(get_config().get("risk_limits", {})).model_dump()
        rendered = json.dumps(policy, ensure_ascii=False, allow_nan=False)
    except (ValidationError, TypeError, ValueError):
        rendered = "invalid policy; no allocation can pass until corrected"
    return ("Allocation policy (caller-configured; null means unconfigured): "
            + rendered
            + ". You may supply an optional typed allocation with final target_weight as a fraction "
              "and absolute stop_loss for the execution instrument, only when dated account NAV, cash, "
              "USD unadjusted per-share quote and explicit limits are provided. Omit allocation rather "
              "than invent inputs. A separate deterministic validator determines whether the proposal "
              "passes; never describe model-generated sizes as approved or validated.")
