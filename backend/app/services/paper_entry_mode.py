"""Choose limit vs market paper entry from live price — no chase, no confirmation bypass."""

from __future__ import annotations

from app.domain.paper_execution_policy import (
    LIMIT_ENTRY_MAX_DISTANCE_BPS,
    MARKET_ENTRY_MAX_DISTANCE_BPS,
    MARKET_ENTRY_MAX_EXECUTION_PENALTY_BPS,
)


def _distance_bps(reference: float, live: float) -> float:
    if reference <= 0:
        return float("inf")
    return abs(live - reference) / reference * 10_000.0


def choose_paper_entry_mode(
    *,
    direction: str,
    entry: float,
    live_price: float | None,
    setup_mode: str | None = None,
) -> tuple[str, str | None, dict]:
    """
    Returns (order_type, skip_reason, diagnostics).
    order_type is 'market' or 'limit'. skip_reason set when entry should not be attempted.
    """
    diag: dict = {
        "planned_entry": entry,
        "live_price": live_price,
        "setup_mode": setup_mode,
    }
    if live_price is None or live_price <= 0:
        return "limit", None, {**diag, "mode_reason": "no_live_price_default_limit"}

    dist_bps = _distance_bps(entry, live_price)
    diag["distance_bps"] = round(dist_bps, 2)

    if dist_bps > LIMIT_ENTRY_MAX_DISTANCE_BPS:
        return "limit", "entry_too_far_from_live", {**diag, "mode_reason": "beyond_limit_max_distance"}

    d = direction.upper()
    favorable_immediate = (d == "LONG" and live_price <= entry) or (d == "SHORT" and live_price >= entry)
    near = dist_bps <= MARKET_ENTRY_MAX_DISTANCE_BPS

    if favorable_immediate or near:
        penalty_bps = dist_bps if favorable_immediate else dist_bps
        if penalty_bps > MARKET_ENTRY_MAX_EXECUTION_PENALTY_BPS:
            if dist_bps <= LIMIT_ENTRY_MAX_DISTANCE_BPS:
                return "limit", None, {**diag, "mode_reason": "market_penalty_use_limit"}
            return "limit", "entry_too_far_from_live", diag
        return "market", None, {**diag, "mode_reason": "confirmed_near_or_favorable_market"}

    # Price on the wrong side of POI — wait for retest via limit (no chase).
    return "limit", None, {**diag, "mode_reason": "poi_retest_limit"}
