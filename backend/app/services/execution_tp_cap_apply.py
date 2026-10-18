"""Apply / re-apply execution TP cap on persisted positions (create + post market fill)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.domain.execution_tp_cap import (
    apply_execution_take_profit_cap,
    gross_reward_r_multiple,
)


def ai_take_profit_2_from_payload(
    structured: dict[str, Any] | None,
    setup_snapshot: dict[str, Any] | None,
) -> float | None:
    """Read optional AI TP2 for audit only — no multi-stage exit logic."""
    for src in (structured or {}, setup_snapshot or {}):
        raw = src.get("take_profit_2")
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val
    return None


def build_execution_tp_extra(
    *,
    tp_cap,
    entry_ref: float,
    direction: str,
    stop_loss: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    from app.domain.execution_tp_cap import EXECUTION_MAX_REWARD_R_MULTIPLE

    now = now or datetime.now(timezone.utc)
    extra: dict[str, Any] = {
        "ai_original_take_profit": tp_cap.ai_take_profit,
        "execution_take_profit": tp_cap.execution_take_profit,
        "execution_tp_cap_max_rr": EXECUTION_MAX_REWARD_R_MULTIPLE,
        "execution_tp_cap_applied_at": now.isoformat(),
        "execution_tp_cap_capped": tp_cap.was_capped,
        "execution_tp_cap_entry_ref": float(entry_ref),
        "execution_gross_rr": gross_reward_r_multiple(
            direction, float(entry_ref), stop_loss, tp_cap.execution_take_profit
        ),
        "ai_gross_rr": gross_reward_r_multiple(
            direction, float(entry_ref), stop_loss, tp_cap.ai_take_profit
        ),
        "execution_take_profit_2_audit_only": True,
    }
    if tp_cap.ai_take_profit_2 is not None and tp_cap.execution_take_profit_2 is not None:
        extra["ai_original_take_profit_2"] = tp_cap.ai_take_profit_2
        extra["execution_take_profit_2"] = tp_cap.execution_take_profit_2
        extra["execution_tp_cap_capped_tp2"] = tp_cap.was_capped_tp2
    return extra


def apply_post_market_fill_execution_tp_cap(
    pos,
    *,
    fill_price: float,
) -> bool:
    """
    Re-cap take_profit after market fill + slippage (new positions only).
    Idempotent per position. Returns True if take_profit changed.
    """
    extra = dict(pos.extra or {})
    if not extra.get("execution_tp_cap_applied_at"):
        return False
    if extra.get("execution_tp_cap_post_fill_applied"):
        return False
    if (pos.order_type or "").lower() != "market":
        return False

    ai_tp = extra.get("ai_original_take_profit")
    if ai_tp is None:
        return False

    tp_cap = apply_execution_take_profit_cap(
        direction=pos.direction,
        entry=float(fill_price),
        stop_loss=float(pos.stop_loss),
        take_profit_ai=float(ai_tp),
        take_profit_2_ai=extra.get("ai_original_take_profit_2"),
    )
    prev_tp = float(pos.take_profit)
    new_tp = float(tp_cap.execution_take_profit)
    pos.take_profit = new_tp

    extra["execution_take_profit"] = new_tp
    extra["execution_tp_cap_post_fill_applied"] = True
    extra["execution_tp_cap_post_fill_at"] = datetime.now(timezone.utc).isoformat()
    extra["execution_tp_cap_post_fill_entry"] = float(fill_price)
    extra["execution_gross_rr_after_fill"] = gross_reward_r_multiple(
        pos.direction, float(fill_price), float(pos.stop_loss), new_tp
    )
    if tp_cap.was_capped or abs(new_tp - float(ai_tp)) > 1e-9:
        extra["execution_tp_cap_capped"] = True
    pos.extra = extra
    return abs(new_tp - prev_tp) > 1e-9
