"""Compare legacy always-limit vs entry-mode policy on stored setup snapshots (not PnL backtest)."""

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
    Entry-policy replay: uses only fields persisted at or before READY promotion
    (`live_price_at_ready`, validation timestamps). Does not simulate fills or PnL.
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
    snapshot_missing = 0

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
        live = vr.get("live_price_at_ready")
        promoted_at = vr.get("promoted_at")
        if live is None:
            snapshot_missing += 1
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
                "promoted_at": promoted_at,
                "live_price_at_ready": live,
                "planned_entry": entry,
                "legacy_would_limit": True,
                "new_order_type": order_type if not skip else None,
                "new_skip": skip,
                "actual_fill_observed": filled,
                "distance_bps": diag.get("distance_bps"),
            }
        )

    n = len(rows)
    uncertainty = n < 20 or actual_fills < 5 or snapshot_missing > max(1, n // 2)

    return {
        "report_type": "entry_policy_compare",
        "not_a_profit_backtest": True,
        "symbol": symbol,
        "sample_setups": n,
        "snapshots_missing_live_price": snapshot_missing,
        "legacy_limit_attempts": legacy_attempts,
        "new_policy": {
            "attempts": new_attempts,
            "skipped_entry_too_far": new_skipped_far,
            "market": new_market,
            "limit": new_limit,
        },
        "observed_fills_in_sample": actual_fills,
        "candidate_to_fill_observed": (actual_fills / legacy_attempts) if legacy_attempts else None,
        "lookahead_policy": (
            "Each row uses stored promotion snapshot only; no future bars or fill simulation."
        ),
        "uncertainty": uncertainty,
        "uncertainty_reason": (
            "Insufficient READY/PENDING samples, missing live_price_at_ready snapshots, or too few observed fills."
            if uncertainty
            else None
        ),
        "details": rows[:50],
    }
