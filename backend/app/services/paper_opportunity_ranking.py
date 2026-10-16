"""Rank multiple READY setups — higher is better; components are not probabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.paper_opportunity_filters import structural_fill_feasibility, structural_ict_quality


@dataclass
class RankBreakdown:
    total: float
    fill_feasibility: float
    ict_quality: float
    net_rr: float
    historical_edge: float
    components: dict[str, Any]


def rank_ready_candidate(
    *,
    order_type: str,
    mode_diag: dict[str, Any],
    net_rr: float,
    structured: dict[str, Any],
    confirmations_complete: bool,
    fill_calibrated_rate: float | None,
    win_calibrated_rate: float | None,
    hours_left: float | None,
) -> RankBreakdown:
    dist = mode_diag.get("distance_bps")
    fill_feas = structural_fill_feasibility(
        order_type=order_type,
        distance_bps=float(dist) if dist is not None else None,
        mode_reason=str(mode_diag.get("mode_reason") or ""),
        hours_left=hours_left,
    )
    ict = structural_ict_quality(
        structured=structured,
        net_rr=net_rr,
        confirmations_complete=confirmations_complete,
    )
    hist = 0.0
    if fill_calibrated_rate is not None:
        hist += max(0.0, fill_calibrated_rate - 0.5) * 40.0
    if win_calibrated_rate is not None:
        hist += max(0.0, win_calibrated_rate - 0.5) * 60.0

    total = (
        fill_feas * 35.0
        + ict * 25.0
        + min(net_rr, 4.0) * 8.0
        + hist
    )
    return RankBreakdown(
        total=round(total, 3),
        fill_feasibility=round(fill_feas * 100, 1),
        ict_quality=round(ict * 100, 1),
        net_rr=round(net_rr, 3),
        historical_edge=round(hist, 3),
        components={
            "fill_feasibility_note": "structural index 0-100, not fill probability",
            "ict_quality_note": "objective ICT/R:R index 0-100, not win probability",
        },
    )


def pick_best_candidate(candidates: list[tuple[Any, RankBreakdown]]) -> tuple[Any, RankBreakdown] | None:
    if not candidates:
        return None
    return max(candidates, key=lambda pair: (pair[1].total, pair[1].net_rr, pair[1].fill_feasibility))
