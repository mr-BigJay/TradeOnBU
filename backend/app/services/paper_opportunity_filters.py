"""Dual independent filters: fill feasibility + win quality (calibrated when data allows)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.paper_execution_policy import (
    HARD_MIN_AUTO_PAPER_NET_RR,
    LIMIT_LOW_FILL_DISTANCE_BPS,
    TARGET_FILL_RATE,
    TARGET_WIN_RATE,
)
from app.models import AiProposedSetup
from app.services.paper_opportunity_calibration import (
    fill_calibration_for_bucket,
    win_calibration_for_bucket,
)


def hours_to_expiry(setup: AiProposedSetup, now: datetime) -> float | None:
    exp = setup.expires_at
    if exp is None:
        return None
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return max(0.0, (exp - now).total_seconds() / 3600.0)


def structural_fill_feasibility(
    *,
    order_type: str,
    distance_bps: float | None,
    mode_reason: str | None,
    hours_left: float | None,
) -> float:
    """Structural fill feasibility index in [0,1] — not a calibrated fill probability."""
    if distance_bps is None:
        return 0.72 if order_type == "limit" else 0.5
    dist = float(distance_bps)
    if order_type == "market":
        if dist <= 10:
            return 0.92
        if dist <= 20:
            return 0.82
        if dist <= 35:
            return 0.72
        return 0.48
    if mode_reason == "beyond_limit_max_distance" or dist > 120:
        return 0.05
    if mode_reason == "poi_retest_limit" and dist > LIMIT_LOW_FILL_DISTANCE_BPS:
        if hours_left is not None and hours_left < 2.0:
            return 0.25
        return 0.42
    if dist <= 25:
        return 0.78
    if dist <= LIMIT_LOW_FILL_DISTANCE_BPS:
        return 0.62
    return 0.38


def structural_ict_quality(
    *,
    structured: dict[str, Any],
    net_rr: float,
    confirmations_complete: bool,
) -> float:
    """ICT + R:R quality index in [0,1] — not a calibrated win rate."""
    score = 0.0
    for key in (
        "liquidity_event_clear",
        "poi_reaction_clear",
        "structural_confirmation_clear",
        "logical_invalidation_clear",
    ):
        if structured.get(key) is True:
            score += 0.2
    if net_rr >= HARD_MIN_AUTO_PAPER_NET_RR:
        score += 0.1
    if net_rr >= 2.5:
        score += 0.05
    if confirmations_complete:
        score += 0.1
    return min(1.0, score)


# Back-compat aliases for tests
heuristic_fill_score = structural_fill_feasibility
heuristic_win_score = structural_ict_quality


async def evaluate_dual_trade_filters(
    session: AsyncSession,
    setup: AiProposedSetup,
    *,
    structured: dict[str, Any],
    order_type: str,
    mode_diag: dict[str, Any],
    net_rr: float,
    live_price: float | None,
    confirmations_complete: bool,
    now: datetime | None = None,
) -> tuple[bool, str | None, dict[str, Any]]:
    """
    Both filters must pass. Returns (ok, reject_reason, selection_audit).
    Never presents structural indices as calibrated probabilities.
    """
    now = now or datetime.now(timezone.utc)
    distance_bps = mode_diag.get("distance_bps")
    mode_reason = mode_diag.get("mode_reason")
    hours_left = hours_to_expiry(setup, now)

    fill_index = structural_fill_feasibility(
        order_type=order_type,
        distance_bps=float(distance_bps) if distance_bps is not None else None,
        mode_reason=str(mode_reason) if mode_reason else None,
        hours_left=hours_left,
    )
    ict_index = structural_ict_quality(
        structured=structured,
        net_rr=net_rr,
        confirmations_complete=confirmations_complete,
    )

    fill_cal = await fill_calibration_for_bucket(
        session,
        direction=setup.direction or "LONG",
        order_type=order_type,
        distance_bps=float(distance_bps) if distance_bps is not None else None,
    )
    win_cal = await win_calibration_for_bucket(
        session,
        direction=setup.direction or "LONG",
        order_type=order_type,
        distance_bps=float(distance_bps) if distance_bps is not None else None,
    )

    fill_pass = False
    win_pass = False
    fill_reason = ""
    win_reason = ""

    if fill_cal.status == "calibrated" and fill_cal.fill_rate is not None:
        fill_pass = fill_cal.fill_rate >= TARGET_FILL_RATE
        fill_reason = "calibrated_fill_rate_meets_target"
    else:
        fill_pass = fill_index >= 0.68 and not (
            order_type == "limit"
            and distance_bps is not None
            and float(distance_bps) > LIMIT_LOW_FILL_DISTANCE_BPS
            and mode_reason == "poi_retest_limit"
        )
        if (
            not fill_pass
            and order_type == "limit"
            and distance_bps is not None
            and float(distance_bps) <= LIMIT_LOW_FILL_DISTANCE_BPS
        ):
            fill_pass = fill_index >= 0.62
        fill_reason = "uncalibrated_structural_fill_feasibility"

    if win_cal.status == "calibrated" and win_cal.win_rate is not None:
        win_pass = win_cal.win_rate >= TARGET_WIN_RATE
        win_reason = "calibrated_win_rate_meets_target"
    elif win_cal.status == "insufficient_closed_samples":
        win_pass = confirmations_complete and net_rr >= HARD_MIN_AUTO_PAPER_NET_RR
        win_reason = "insufficient_closed_samples_objective_ict_only"
    else:
        flags_ok = all(
            structured.get(k) is True
            for k in (
                "liquidity_event_clear",
                "poi_reaction_clear",
                "structural_confirmation_clear",
                "logical_invalidation_clear",
            )
        )
        if confirmations_complete and net_rr >= HARD_MIN_AUTO_PAPER_NET_RR:
            win_pass = True
            win_reason = "uncalibrated_objective_ict_confirmed_net_rr"
        else:
            win_pass = ict_index >= 0.75 and flags_ok and net_rr >= HARD_MIN_AUTO_PAPER_NET_RR
            win_reason = "uncalibrated_structural_ict_quality"

    audit: dict[str, Any] = {
        "fill_calibration_status": fill_cal.status,
        "win_calibration_status": win_cal.status,
        "calibration_bucket": fill_cal.bucket,
        "fill_filter": {
            "pass": fill_pass,
            "reason": fill_reason,
            "structural_feasibility_index": round(fill_index * 100, 1),
            "structural_index_disclaimer": "not a probability",
            "calibrated": {
                "orders_sample": fill_cal.orders_sample,
                "filled_count": fill_cal.filled_count,
                "rate": fill_cal.fill_rate,
                "ci_low": fill_cal.ci_low,
                "ci_high": fill_cal.ci_high,
                "target": fill_cal.target,
                "walk_forward_oos_rate": fill_cal.walk_forward_oos_rate,
                "walk_forward_oos_n": fill_cal.walk_forward_oos_n,
            },
        },
        "win_filter": {
            "pass": win_pass,
            "reason": win_reason,
            "structural_quality_index": round(ict_index * 100, 1),
            "structural_index_disclaimer": "not a probability",
            "calibrated": {
                "filled_sample": win_cal.filled_sample,
                "closed_sample": win_cal.closed_sample,
                "win_count": win_cal.win_count,
                "rate": win_cal.win_rate,
                "ci_low": win_cal.ci_low,
                "ci_high": win_cal.ci_high,
                "target": win_cal.target,
                "walk_forward_oos_rate": win_cal.walk_forward_oos_rate,
                "walk_forward_oos_n": win_cal.walk_forward_oos_n,
            },
        },
        "order_type": order_type,
        "distance_bps": distance_bps,
        "net_rr": net_rr,
        "live_price": live_price,
        "ict_confirmed_flags": {
            k: structured.get(k)
            for k in (
                "liquidity_event_clear",
                "poi_reaction_clear",
                "structural_confirmation_clear",
                "logical_invalidation_clear",
            )
        },
        "selection_rationale_fa": _rationale_fa(
            order_type=order_type,
            fill_pass=fill_pass,
            win_pass=win_pass,
            fill_cal_status=fill_cal.status,
            win_cal_status=win_cal.status,
            net_rr=net_rr,
        ),
    }

    if not fill_pass:
        return False, "low_fill_probability", audit
    if not win_pass:
        return False, "low_win_probability", audit
    return True, None, audit


def _rationale_fa(
    *,
    order_type: str,
    fill_pass: bool,
    win_pass: bool,
    fill_cal_status: str,
    win_cal_status: str,
    net_rr: float,
) -> str:
    parts = [
        f"ورود {order_type.upper()}",
        f"R:R خالص {net_rr:.2f}",
        f"Fill cal: {fill_cal_status}",
        f"Win cal: {win_cal_status}",
    ]
    if fill_pass and win_pass:
        parts.append("عبور از هر دو فیلتر Fill و Win")
    return " · ".join(parts)


def persist_selection_audit(setup: AiProposedSetup, audit: dict[str, Any]) -> None:
    notes = dict(setup.objective_notes or {})
    notes["selection_audit"] = audit
    notes["selection_audit_at"] = datetime.now(timezone.utc).isoformat()
    setup.objective_notes = notes
