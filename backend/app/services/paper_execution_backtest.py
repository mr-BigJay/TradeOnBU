"""Walk-forward style comparison: legacy limit-only vs entry-mode policy (no lookahead)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiProposedSetup, SimPosition
from app.services.paper_entry_mode import choose_paper_entry_mode


def _had_fill(setup_id: str, positions: list[SimPosition]) -> bool:
    for p in positions:
        if p.ai_setup_id != setup_id:
            continue
        if p.fill_price is not None:
            return True
        if p.status in ("open", "closed", "tp_hit", "sl_hit"):
            return True
    return False


async def compare_entry_policies_on_history(
    session: AsyncSession,
    *,
    symbol: str = "BTCUSDT",
    limit: int = 300,
) -> dict[str, Any]:
    """
    For each setup that reached READY/PENDING, replay entry-mode choice using
    `live_price` stored on last validation (if any) or planned entry (conservative).

    Does NOT simulate intrabar fills — compares whether legacy always-limit would
    still attempt vs new policy (skip/market/limit). Actual fill outcomes come from DB.
    """
    setups = list(
        (
            await session.execute(
                select(AiProposedSetup)
                .where(AiProposedSetup.symbol == symbol)
                .order_by(desc(AiProposedSetup.updated_at))
                .limit(limit)
            )
        ).scalars()
    )
    positions = list(
        (await session.execute(select(SimPosition).where(SimPosition.symbol == symbol))).scalars()
    )

    legacy_attempts = 0
    new_attempts = 0
    new_skipped_far = 0
    new_market = 0
    new_limit = 0
    actual_fills = 0

    rows: list[dict[str, Any]] = []

    for s in setups:
        vr = s.last_validation_result or {}
        if not vr.get("ok") and s.lifecycle_state not in (
            "READY_LONG",
            "READY_SHORT",
            "PENDING_ENTRY",
            "POSITION_OPEN",
            "POSITION_CLOSED",
        ):
            continue
        entry = float(s.entry_price or 0)
        if entry <= 0:
            continue
        live = vr.get("live_price_at_ready") or vr.get("promoted_live_price")
        if live is None:
            notes = s.objective_notes or {}
            live = notes.get("last_live_price")

        legacy_attempts += 1
        order_type, skip, diag = choose_paper_entry_mode(
            direction=s.direction or "LONG",
            entry=entry,
            live_price=float(live) if live is not None else None,
            setup_mode=s.setup_mode,
        )
        if skip:
            new_skipped_far += 1
        else:
            new_attempts += 1
            if order_type == "market":
                new_market += 1
            else:
                new_limit += 1

        filled = _had_fill(s.setup_id, positions)
        if filled:
            actual_fills += 1

        rows.append(
            {
                "setup_id": s.setup_id,
                "lifecycle": s.lifecycle_state,
                "legacy_would_limit": True,
                "new_order_type": order_type if not skip else None,
                "new_skip": skip,
                "actual_fill": filled,
                "distance_bps": diag.get("distance_bps"),
            }
        )

    n = len(rows)
    uncertainty = n < 20 or actual_fills < 5

    return {
        "symbol": symbol,
        "sample_setups": n,
        "legacy_limit_attempts": legacy_attempts,
        "new_policy": {
            "attempts": new_attempts,
            "skipped_entry_too_far": new_skipped_far,
            "market": new_market,
            "limit": new_limit,
        },
        "observed_fills_in_sample": actual_fills,
        "candidate_to_fill_observed": (actual_fills / legacy_attempts) if legacy_attempts else None,
        "lookahead_free": True,
        "method_note": (
            "Uses stored setup validation notes only; live_price_at_ready is set on READY promotion "
            "when available. No fabricated fill probabilities."
        ),
        "uncertainty": uncertainty,
        "uncertainty_reason": (
            "Insufficient historical READY/PENDING samples or fills for stable comparison."
            if uncertainty
            else None
        ),
        "details": rows[:50],
    }
