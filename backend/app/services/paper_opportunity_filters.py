"""Dual independent filters: fill probability + win probability (calibrated when data allows)."""

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
from app.services.paper_opportunity_calibration import calibrated_rates_for_bucket


def _hours_to_expiry(setup: AiProposedSetup, now: datetime) -> float | None:
    exp = setup.expires_at
    if exp is None:
        return None
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return max(0.0, (exp - now).total_seconds() / 3600.0)


def heuristic_fill_score(
    *,
    order_type: str,
    distance_bps: float | None,
    mode_reason: str | None,
    hours_left: float | None,
) -> float:
    """Structural fill feasibility score in [0,1] — not a calibrated market probability."""
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


def heuristic_win_score(
    *,
    structured: dict[str, Any],
    net_rr: float,
    confirmations_complete: bool,
) -> float:
    """ICT + R:R quality proxy in [0,1] — not calibrated win rate."""
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
    Never claims calibrated probability when sample size is insufficient.
    """
    now = now or datetime.now(timezone.utc)
    distance_bps = mode_diag.get("distance_bps")
    mode_reason = mode_diag.get("mode_reason")
    hours_left = _hours_to_expiry(setup, now)

    fill_heur = heuristic_fill_score(
        order_type=order_type,
        distance_bps=float(distance_bps) if distance_bps is not None else None,
        mode_reason=str(mode_reason) if mode_reason else None,
        hours_left=hours_left,
    )
    win_heur = heuristic_win_score(
        structured=structured,
        net_rr=net_rr,
        confirmations_complete=confirmations_complete,
    )

    cal = await calibrated_rates_for_bucket(
        session,
        direction=setup.direction or "LONG",
        order_type=order_type,
        distance_bps=float(distance_bps) if distance_bps is not None else None,
    )

    calibration_status: Literal["calibrated", "uncalibrated"] = cal.status
    fill_pass = False
    win_pass = False
    fill_reason = ""
    win_reason = ""

    if cal.status == "calibrated" and cal.fill_rate is not None:
        fill_pass = cal.fill_rate >= TARGET_FILL_RATE
        fill_reason = f"calibrated_fill_rate={cal.fill_rate:.2f} target={TARGET_FILL_RATE}"
    else:
        fill_pass = fill_heur >= 0.68 and not (
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
            fill_pass = fill_heur >= 0.62
        fill_reason = f"uncalibrated_heuristic_fill={fill_heur:.2f}"

    if cal.status == "calibrated" and cal.win_rate is not None:
        win_pass = cal.win_rate >= TARGET_WIN_RATE
        win_reason = f"calibrated_win_rate={cal.win_rate:.2f} target={TARGET_WIN_RATE}"
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
            win_pass = win_heur >= 0.75 and flags_ok and net_rr >= HARD_MIN_AUTO_PAPER_NET_RR
            win_reason = f"uncalibrated_heuristic_win={win_heur:.2f}"

    audit: dict[str, Any] = {
        "calibration_status": calibration_status,
        "calibration_bucket": cal.bucket,
        "calibration_sample_size": cal.sample_size,
        "fill_filter": {
            "pass": fill_pass,
            "reason": fill_reason,
            "heuristic_score": round(fill_heur, 3),
            "calibrated_rate": cal.fill_rate,
            "target_rate": TARGET_FILL_RATE,
        },
        "win_filter": {
            "pass": win_pass,
            "reason": win_reason,
            "heuristic_score": round(win_heur, 3),
            "calibrated_rate": cal.win_rate,
            "target_rate": TARGET_WIN_RATE,
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
            calibration_status=calibration_status,
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
    calibration_status: str,
    net_rr: float,
) -> str:
    parts = [
        f"ورود {order_type.upper()}",
        f"R:R خالص {net_rr:.2f}",
        f"کالیبراسیون: {calibration_status}",
    ]
    if fill_pass and win_pass:
        parts.append("عبور از هر دو فیلتر Fill و Win")
    return " · ".join(parts)


def persist_selection_audit(setup: AiProposedSetup, audit: dict[str, Any]) -> None:
    notes = dict(setup.objective_notes or {})
    notes["selection_audit"] = audit
    notes["selection_audit_at"] = datetime.now(timezone.utc).isoformat()
    setup.objective_notes = notes
