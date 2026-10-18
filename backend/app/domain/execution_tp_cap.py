"""Execution-layer cap: reward:risk at fill must not exceed 1:3 (AI levels unchanged upstream)."""

from __future__ import annotations

from dataclasses import dataclass

EXECUTION_MAX_REWARD_R_MULTIPLE: float = 3.0


@dataclass(frozen=True)
class ExecutionTpCapResult:
    ai_take_profit: float
    execution_take_profit: float
    ai_take_profit_2: float | None
    execution_take_profit_2: float | None
    risk: float
    was_capped: bool
    was_capped_tp2: bool


def _cap_single_tp(
    *,
    direction: str,
    entry: float,
    risk: float,
    tp_ai: float,
) -> tuple[float, bool]:
    d = direction.upper()
    if d == "LONG":
        cap_level = entry + EXECUTION_MAX_REWARD_R_MULTIPLE * risk
        effective = min(tp_ai, cap_level)
    elif d == "SHORT":
        cap_level = entry - EXECUTION_MAX_REWARD_R_MULTIPLE * risk
        effective = max(tp_ai, cap_level)
    else:
        return tp_ai, False
    capped = abs(effective - tp_ai) > 1e-9
    return effective, capped


def apply_execution_take_profit_cap(
    *,
    direction: str,
    entry: float,
    stop_loss: float,
    take_profit_ai: float,
    take_profit_2_ai: float | None = None,
) -> ExecutionTpCapResult:
    """
    Cap execution TP(s) to 3R from entry/SL. Entry and SL are not modified.
    Invalid geometry returns AI values unchanged (create_order validation rejects separately).
    """
    if entry <= 0 or take_profit_ai <= 0 or stop_loss <= 0:
        return ExecutionTpCapResult(
            ai_take_profit=take_profit_ai,
            execution_take_profit=take_profit_ai,
            ai_take_profit_2=take_profit_2_ai,
            execution_take_profit_2=take_profit_2_ai,
            risk=0.0,
            was_capped=False,
            was_capped_tp2=False,
        )

    risk = abs(entry - stop_loss)
    if risk <= 0:
        return ExecutionTpCapResult(
            ai_take_profit=take_profit_ai,
            execution_take_profit=take_profit_ai,
            ai_take_profit_2=take_profit_2_ai,
            execution_take_profit_2=take_profit_2_ai,
            risk=0.0,
            was_capped=False,
            was_capped_tp2=False,
        )

    tp1, capped1 = _cap_single_tp(direction=direction, entry=entry, risk=risk, tp_ai=take_profit_ai)
    tp2_exec: float | None = None
    capped2 = False
    if take_profit_2_ai is not None and take_profit_2_ai > 0:
        tp2_exec, capped2 = _cap_single_tp(
            direction=direction, entry=entry, risk=risk, tp_ai=take_profit_2_ai
        )

    return ExecutionTpCapResult(
        ai_take_profit=take_profit_ai,
        execution_take_profit=tp1,
        ai_take_profit_2=take_profit_2_ai,
        execution_take_profit_2=tp2_exec,
        risk=risk,
        was_capped=capped1,
        was_capped_tp2=capped2,
    )


def gross_reward_r_multiple(direction: str, entry: float, stop_loss: float, take_profit: float) -> float:
    risk = abs(entry - stop_loss)
    if risk <= 0:
        return 0.0
    reward = abs(take_profit - entry)
    return reward / risk
