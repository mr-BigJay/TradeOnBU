"""Apply / re-apply execution TP cap on persisted positions (create + post fill)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.domain.execution_tp_cap import (
    apply_execution_take_profit_cap,
    gross_reward_r_multiple,
)


def execution_tp_cap_enabled(extra: dict[str, Any] | None) -> bool:
    """New positions only — legacy rows without this metadata are never touched."""
    return bool((extra or {}).get("execution_tp_cap_applied_at"))


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


def levels_geometry_valid(
    direction: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
) -> tuple[bool, str | None]:
    d = (direction or "").upper()
    if d == "LONG":
        if not (stop_loss < entry < take_profit):
            return False, "long_fill_geometry_invalid"
    elif d == "SHORT":
        if not (take_profit < entry < stop_loss):
            return False, "short_fill_geometry_invalid"
    else:
        return False, "invalid_direction"
    return True, None


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


@dataclass
class PostFillCapResult:
    applied: bool
    take_profit: float
    extra: dict[str, Any]
    geometry_ok: bool
    geometry_reason: str | None
    tp_changed: bool


def compute_post_fill_execution_tp(
    pos,
    *,
    fill_price: float,
) -> PostFillCapResult:
    """
    Re-cap execution TP (+ audit TP2) from actual fill price (market or limit).
    Legacy positions: no cap, no geometry-based cancel. Idempotent after first post-fill cap.
    """
    extra = dict(pos.extra or {})
    base_tp = float(pos.take_profit)

    if not execution_tp_cap_enabled(extra):
        return PostFillCapResult(
            applied=False,
            take_profit=base_tp,
            extra=extra,
            geometry_ok=True,
            geometry_reason=None,
            tp_changed=False,
        )

    if extra.get("execution_tp_cap_post_fill_applied"):
        ok, reason = levels_geometry_valid(pos.direction, fill_price, float(pos.stop_loss), base_tp)
        return PostFillCapResult(
            applied=False,
            take_profit=base_tp,
            extra=extra,
            geometry_ok=ok,
            geometry_reason=reason,
            tp_changed=False,
        )

    ai_tp = extra.get("ai_original_take_profit")
    if ai_tp is None:
        ok, reason = levels_geometry_valid(pos.direction, fill_price, float(pos.stop_loss), base_tp)
        return PostFillCapResult(
            applied=False,
            take_profit=base_tp,
            extra=extra,
            geometry_ok=ok,
            geometry_reason=reason,
            tp_changed=False,
        )

    tp_cap = apply_execution_take_profit_cap(
        direction=pos.direction,
        entry=float(fill_price),
        stop_loss=float(pos.stop_loss),
        take_profit_ai=float(ai_tp),
        take_profit_2_ai=extra.get("ai_original_take_profit_2"),
    )
    new_tp = float(tp_cap.execution_take_profit)
    extra["execution_take_profit"] = new_tp
    extra["execution_tp_cap_post_fill_applied"] = True
    extra["execution_tp_cap_post_fill_at"] = datetime.now(timezone.utc).isoformat()
    extra["execution_tp_cap_post_fill_entry"] = float(fill_price)
    extra["execution_gross_rr_after_fill"] = gross_reward_r_multiple(
        pos.direction, float(fill_price), float(pos.stop_loss), new_tp
    )
    if tp_cap.was_capped or abs(new_tp - float(ai_tp)) > 1e-9:
        extra["execution_tp_cap_capped"] = True
    if tp_cap.execution_take_profit_2 is not None:
        extra["execution_take_profit_2"] = float(tp_cap.execution_take_profit_2)
        extra["execution_tp_cap_capped_tp2"] = tp_cap.was_capped_tp2

    ok, reason = levels_geometry_valid(pos.direction, fill_price, float(pos.stop_loss), new_tp)
    return PostFillCapResult(
        applied=True,
        take_profit=new_tp,
        extra=extra,
        geometry_ok=ok,
        geometry_reason=reason,
        tp_changed=abs(new_tp - base_tp) > 1e-9,
    )


# Back-compat alias
compute_post_market_fill_execution_tp = compute_post_fill_execution_tp


def apply_post_market_fill_execution_tp_cap(
    pos,
    *,
    fill_price: float,
) -> bool:
    result = compute_post_fill_execution_tp(pos, fill_price=fill_price)
    if not result.geometry_ok:
        return False
    prev = float(pos.take_profit)
    pos.take_profit = result.take_profit
    pos.extra = result.extra
    return result.applied and abs(result.take_profit - prev) > 1e-9
