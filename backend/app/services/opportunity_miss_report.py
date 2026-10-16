"""Classify setups into miss-audit outcome buckets with evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.opportunity_miss_reasons import (
    ENGINE_DELAY_THRESHOLD_MS,
    OUTCOME_AMBIGUOUS_CHRONOLOGY,
    OUTCOME_CONFIRMATION_INCOMPLETE,
    OUTCOME_ENGINE_DELAY,
    OUTCOME_INSUFFICIENT_REPLAY_HISTORY,
    OUTCOME_INTENTIONAL_WAIT,
    OUTCOME_INVALIDATED,
    OUTCOME_LIMIT_NOT_FILLED,
    OUTCOME_MARKET_DATA_GAP_STALE,
    OUTCOME_NET_RR_BELOW_MIN,
    OUTCOME_ORDER_CREATED,
    OUTCOME_ORDER_REJECTED,
    OUTCOME_POTENTIAL_MISSED,
    OUTCOME_REPLAY_SEQUENCE_LIMITED,
    OUTCOME_TERMINAL_OTHER,
    REPLAY_STATUS_INSUFFICIENT_HISTORY,
    REPLAY_STATUS_OK,
    REPLAY_STATUS_SEQUENCE_LIMITED,
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
from app.models import AiProposedSetup
from app.services.opportunity_timeline import build_setup_timeline


def _parse_iso_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _had_real_fill(paper_orders: list[dict[str, Any]]) -> bool:
    for p in paper_orders:
        if p.get("fill_price") is not None:
            return True
        st = (p.get("status") or "").lower()
        if st in ("open", "tp_hit", "sl_hit", "closed") and p.get("fill_price") is not None:
            return True
    return False


def _engine_delay_ms(timeline: dict[str, Any]) -> int | None:
    """Delay from final confirmation bar close to READY (or order attempt if sooner)."""
    final_close = timeline.get("final_confirmation_bar_close_ms")
    ready_ms = _parse_iso_ms(timeline.get("ready_at"))
    if final_close is None or ready_ms is None:
        return None
    return max(0, ready_ms - int(final_close))


def _live_replay_aligned(replay_diff: dict[str, Any] | None) -> bool:
    if not replay_diff:
        return False
    if not _replay_trusted(replay_diff):
        return False
    if replay_diff.get("missing_in_live") or replay_diff.get("extra_in_live"):
        return False
    if replay_diff.get("order_mismatch"):
        return False
    return True


def chronology_advisories(replay_diff: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not replay_diff:
        return []
    amb = [
        c
        for c in replay_diff.get("same_bar_multi_token_candidates") or []
        if len(c.get("tokens_on_5m_close_window") or []) > 1 and not c.get("chronology_provable_on_1m")
    ]
    if not amb:
        return []
    return [
        {
            "kind": "ambiguous_chronology",
            "bar_open_ms": c.get("bar_open_ms"),
            "tokens_on_5m_close_window": c.get("tokens_on_5m_close_window"),
            "chronology_reason": c.get("chronology_reason"),
        }
        for c in amb
    ]


def _replay_trusted(replay_diff: dict[str, Any] | None) -> bool:
    if not replay_diff:
        return False
    if replay_diff.get("replay_trusted") is False:
        return False
    return replay_diff.get("replay_status", REPLAY_STATUS_OK) == REPLAY_STATUS_OK


def classify_setup_outcome(setup: AiProposedSetup, *, timeline: dict[str, Any], replay_diff: dict[str, Any] | None) -> str:
    lc = (setup.lifecycle_state or "").upper()
    reason = setup.last_rejection_reason or ""
    notes = setup.objective_notes or {}
    health = notes.get("market_data_health") or {}
    pos_hint = timeline.get("paper_orders") or []

    if replay_diff:
        rs = replay_diff.get("replay_status")
        if rs == REPLAY_STATUS_INSUFFICIENT_HISTORY:
            return OUTCOME_INSUFFICIENT_REPLAY_HISTORY
        if rs == REPLAY_STATUS_SEQUENCE_LIMITED:
            return OUTCOME_REPLAY_SEQUENCE_LIMITED

    if _had_real_fill(pos_hint) or lc in (POSITION_OPEN, POSITION_CLOSED):
        if lc == PENDING_ENTRY and not _had_real_fill(pos_hint):
            pass
        else:
            return OUTCOME_ORDER_CREATED

    if lc == PENDING_ENTRY:
        return OUTCOME_ORDER_CREATED

    if lc == INVALIDATED or reason == REASON_STRUCTURAL_INVALIDATION:
        return OUTCOME_INVALIDATED
    if lc == EXPIRED or reason == REASON_SETUP_EXPIRED:
        return OUTCOME_TERMINAL_OTHER
    if reason in (REASON_NET_RR_BELOW_MIN, REASON_ICT_CONFIRMED_BLOCKED_RR):
        return OUTCOME_NET_RR_BELOW_MIN
    if lc in (ICT_CONFIRMED_LONG, ICT_CONFIRMED_SHORT):
        return OUTCOME_NET_RR_BELOW_MIN
    if reason in (REASON_MISSING_MARKET_DATA, REASON_STALE_MARKET_DATA) or health.get("ok") is False:
        return OUTCOME_MARKET_DATA_GAP_STALE

    if replay_diff and chronology_advisories(replay_diff):
        if _replay_trusted(replay_diff) and not _live_replay_aligned(replay_diff):
            return OUTCOME_AMBIGUOUS_CHRONOLOGY

    if reason == REASON_MISSING_CONFIRMATIONS or lc in (WAIT_LONG, WAIT_SHORT):
        if _replay_trusted(replay_diff) and (
            replay_diff.get("missing_in_live") or replay_diff.get("order_mismatch")
        ):
            return OUTCOME_POTENTIAL_MISSED
        return OUTCOME_CONFIRMATION_INCOMPLETE

    if lc in (READY_LONG, READY_SHORT):
        skip = (setup.last_validation_result or {}).get("order_skipped_reason")
        if skip:
            return OUTCOME_ORDER_REJECTED
        delay = _engine_delay_ms(timeline)
        if delay is not None and delay > ENGINE_DELAY_THRESHOLD_MS:
            return OUTCOME_ENGINE_DELAY
        return OUTCOME_INTENTIONAL_WAIT

    if pos_hint and not _had_real_fill(pos_hint):
        if all((p.get("status") or "").lower() in ("cancelled", "expired") for p in pos_hint):
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

    bucket_keys = (
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
        OUTCOME_INSUFFICIENT_REPLAY_HISTORY,
        OUTCOME_REPLAY_SEQUENCE_LIMITED,
        OUTCOME_INTENTIONAL_WAIT,
        OUTCOME_TERMINAL_OTHER,
    )
    buckets: dict[str, list[dict[str, Any]]] = {k: [] for k in bucket_keys}

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
            "chronology_advisories": chronology_advisories(replay_diff),
        }
        buckets.setdefault(outcome, []).append(entry)
        rows.append(entry)

    summary = {k: len(v) for k, v in buckets.items()}
    summary["total_setups"] = len(rows)
    summary["potential_missed_count"] = summary.get(OUTCOME_POTENTIAL_MISSED, 0)
    summary["insufficient_replay_history_count"] = summary.get(OUTCOME_INSUFFICIENT_REPLAY_HISTORY, 0)
    summary["replay_unavailable_count"] = (
        summary.get(OUTCOME_INSUFFICIENT_REPLAY_HISTORY, 0)
        + summary.get(OUTCOME_REPLAY_SEQUENCE_LIMITED, 0)
        + summary.get(OUTCOME_AMBIGUOUS_CHRONOLOGY, 0)
    )
    summary["intentional_safety_blocks"] = (
        summary.get(OUTCOME_NET_RR_BELOW_MIN, 0)
        + summary.get(OUTCOME_INVALIDATED, 0)
        + summary.get(OUTCOME_CONFIRMATION_INCOMPLETE, 0)
    )
    summary["data_not_zero_missed_note"] = (
        "potential_missed_count excludes insufficient/ambiguous replay; see replay_unavailable_count"
    )

    return {
        "summary": summary,
        "by_outcome": buckets,
        "rows": rows,
    }
