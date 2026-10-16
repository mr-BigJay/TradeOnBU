"""Classify setups into miss-audit outcome buckets with evidence."""

from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.opportunity_miss_reasons import (
    OUTCOME_AMBIGUOUS_CHRONOLOGY,
    OUTCOME_CONFIRMATION_INCOMPLETE,
    OUTCOME_ENGINE_DELAY,
    OUTCOME_INTENTIONAL_WAIT,
    OUTCOME_INVALIDATED,
    OUTCOME_LIMIT_NOT_FILLED,
    OUTCOME_MARKET_DATA_GAP_STALE,
    OUTCOME_NET_RR_BELOW_MIN,
    OUTCOME_ORDER_CREATED,
    OUTCOME_ORDER_REJECTED,
    OUTCOME_POTENTIAL_MISSED,
    OUTCOME_TERMINAL_OTHER,
)
from app.domain.setup_lifecycle import (
    EXPIRED,
    INVALIDATED,
    PENDING_ENTRY,
    POSITION_CLOSED,
    POSITION_OPEN,
    READY_LONG,
    READY_SHORT,
    REASON_ICT_CONFIRMED_BLOCKED_RR,
    REASON_MISSING_CONFIRMATIONS,
    REASON_MISSING_MARKET_DATA,
    REASON_NET_RR_BELOW_MIN,
    REASON_SETUP_EXPIRED,
    REASON_STALE_MARKET_DATA,
    REASON_STRUCTURAL_INVALIDATION,
    WAIT_LONG,
    WAIT_SHORT,
    ICT_CONFIRMED_LONG,
    ICT_CONFIRMED_SHORT,
)
from app.models import AiProposedSetup, SimPosition
from app.services.opportunity_timeline import build_setup_timeline


def classify_setup_outcome(setup: AiProposedSetup, *, timeline: dict[str, Any], replay_diff: dict[str, Any] | None) -> str:
    lc = (setup.lifecycle_state or "").upper()
    reason = setup.last_rejection_reason or ""
    notes = setup.objective_notes or {}
    health = notes.get("market_data_health") or {}

    if lc in (POSITION_OPEN,):
        return OUTCOME_ORDER_CREATED
    if lc == PENDING_ENTRY:
        return OUTCOME_ORDER_CREATED
    pos_hint = timeline.get("paper_orders") or []
    if any(p.get("status") in ("open", "pending_entry") for p in pos_hint):
        if lc not in (PENDING_ENTRY, POSITION_OPEN):
            return OUTCOME_POTENTIAL_MISSED

    if lc == INVALIDATED or reason == REASON_STRUCTURAL_INVALIDATION:
        return OUTCOME_INVALIDATED
    if lc == EXPIRED or reason == REASON_SETUP_EXPIRED:
        return OUTCOME_TERMINAL_OTHER
    if reason in (REASON_NET_RR_BELOW_MIN, REASON_ICT_CONFIRMED_BLOCKED_RR):
        return OUTCOME_NET_RR_BELOW_MIN
    if reason in (REASON_MISSING_MARKET_DATA, REASON_STALE_MARKET_DATA) or health.get("ok") is False:
        return OUTCOME_MARKET_DATA_GAP_STALE
    if reason == REASON_MISSING_CONFIRMATIONS or lc in (WAIT_LONG, WAIT_SHORT):
        if replay_diff and (replay_diff.get("missing_in_live") or replay_diff.get("order_mismatch")):
            return OUTCOME_POTENTIAL_MISSED
        return OUTCOME_CONFIRMATION_INCOMPLETE
    if lc in (ICT_CONFIRMED_LONG, ICT_CONFIRMED_SHORT):
        return OUTCOME_NET_RR_BELOW_MIN
    if lc in (READY_LONG, READY_SHORT):
        skip = (setup.last_validation_result or {}).get("order_skipped_reason")
        if skip:
            return OUTCOME_ORDER_REJECTED
        return OUTCOME_ENGINE_DELAY

    if replay_diff and replay_diff.get("same_bar_multi_token_candidates"):
        amb = [
            c
            for c in replay_diff["same_bar_multi_token_candidates"]
            if len(c.get("tokens_on_5m_close_window") or []) > 1 and not c.get("chronology_provable_on_1m")
        ]
        if amb:
            return OUTCOME_AMBIGUOUS_CHRONOLOGY

    if pos_hint and all(p.get("status") in ("cancelled", "closed", "sl_hit", "tp_hit") for p in pos_hint):
        return OUTCOME_LIMIT_NOT_FILLED

    return OUTCOME_INTENTIONAL_WAIT


async def build_miss_audit_report(
    session: AsyncSession,
    *,
    symbol: str | None = None,
    limit: int = 100,
    replay_by_setup: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    q = select(AiProposedSetup).order_by(desc(AiProposedSetup.updated_at)).limit(limit)
    if symbol:
        q = q.where(AiProposedSetup.symbol == symbol)
    setups = list((await session.execute(q)).scalars())

    buckets: dict[str, list[dict[str, Any]]] = {k: [] for k in (
        OUTCOME_ORDER_CREATED,
        OUTCOME_CONFIRMATION_INCOMPLETE,
        OUTCOME_INVALIDATED,
        OUTCOME_NET_RR_BELOW_MIN,
        OUTCOME_MARKET_DATA_GAP_STALE,
        OUTCOME_ENGINE_DELAY,
        OUTCOME_ORDER_REJECTED,
        OUTCOME_LIMIT_NOT_FILLED,
        OUTCOME_POTENTIAL_MISSED,
        OUTCOME_AMBIGUOUS_CHRONOLOGY,
        OUTCOME_INTENTIONAL_WAIT,
        OUTCOME_TERMINAL_OTHER,
    )}

    replay_by_setup = replay_by_setup or {}
    rows: list[dict[str, Any]] = []
    for setup in setups:
        timeline = await build_setup_timeline(session, setup)
        replay_diff = replay_by_setup.get(setup.setup_id)
        outcome = classify_setup_outcome(setup, timeline=timeline, replay_diff=replay_diff)
        entry = {
            "setup_id": setup.setup_id,
            "outcome": outcome,
            "lifecycle_state": setup.lifecycle_state,
            "rejection_reason": setup.last_rejection_reason,
            "timeline": timeline,
            "replay_diff": replay_diff,
        }
        buckets.setdefault(outcome, []).append(entry)
        rows.append(entry)

    summary = {k: len(v) for k, v in buckets.items()}
    summary["total_setups"] = len(rows)
    summary["potential_missed_count"] = summary.get(OUTCOME_POTENTIAL_MISSED, 0)
    summary["intentional_safety_blocks"] = (
        summary.get(OUTCOME_NET_RR_BELOW_MIN, 0)
        + summary.get(OUTCOME_INVALIDATED, 0)
        + summary.get(OUTCOME_CONFIRMATION_INCOMPLETE, 0)
        + summary.get(OUTCOME_AMBIGUOUS_CHRONOLOGY, 0)
    )

    return {
        "summary": summary,
        "by_outcome": buckets,
        "rows": rows,
    }
