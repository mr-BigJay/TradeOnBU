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
    REPLAY_STATUS_NOT_RUN,
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
from app.domain.entry_execution import (
    AUDIT_DIRECTION_CORRECT,
    AUDIT_EXECUTED_LOSS,
    AUDIT_EXECUTED_OPEN,
    AUDIT_EXECUTED_PROFIT,
    AUDIT_MISSED_EXECUTABLE,
    AUDIT_ORDER_UNFILLED,
    AUDIT_POTENTIAL_NOT_EXECUTABLE,
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


def derive_replay_status(replay_diff: dict[str, Any] | None) -> str:
    """Replay audit axis (independent of trading / execution outcome)."""
    if not replay_diff:
        return REPLAY_STATUS_NOT_RUN
    return str(replay_diff.get("replay_status") or REPLAY_STATUS_NOT_RUN)


def classify_execution_outcome(
    setup: AiProposedSetup,
    *,
    timeline: dict[str, Any],
    replay_diff: dict[str, Any] | None,
) -> str:
    """Why the setup did or did not become an order — never overridden by replay availability."""
    lc = (setup.lifecycle_state or "").upper()
    reason = setup.last_rejection_reason or ""
    notes = setup.objective_notes or {}
    health = notes.get("market_data_health") or {}
    pos_hint = timeline.get("paper_orders") or []

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


def classify_setup_outcome(
    setup: AiProposedSetup,
    *,
    timeline: dict[str, Any],
    replay_diff: dict[str, Any] | None,
) -> str:
    """Backward-compatible alias for ``classify_execution_outcome``."""
    return classify_execution_outcome(setup, timeline=timeline, replay_diff=replay_diff)


def _closed_net_pnl(pos: dict[str, Any]) -> float | None:
    pnl = pos.get("net_pnl_usdt")
    if pnl is None:
        return None
    try:
        return float(pnl)
    except (TypeError, ValueError):
        return None


def classify_user_audit_bucket(
    setup: AiProposedSetup,
    *,
    timeline: dict[str, Any],
    execution_outcome: str,
) -> str:
    """
    User-facing buckets with strict PnL: closed != TP hit; profit requires net_pnl > 0.
    """
    pos_hint = timeline.get("paper_orders") or []
    filled = _had_real_fill(pos_hint)
    pipe = (setup.objective_notes or {}).get("execution_pipeline") or {}

    if filled:
        closed_rows = [
            p
            for p in pos_hint
            if (p.get("status") or "").lower() in ("closed", "tp_hit", "sl_hit")
            and p.get("fill_price") is not None
        ]
        if closed_rows:
            pnl = _closed_net_pnl(closed_rows[-1])
            if pnl is not None:
                if pnl > 0:
                    return AUDIT_EXECUTED_PROFIT
                if pnl < 0:
                    return AUDIT_EXECUTED_LOSS
                return AUDIT_EXECUTED_OPEN
        open_rows = [p for p in pos_hint if (p.get("status") or "").lower() == "open"]
        if open_rows:
            return AUDIT_EXECUTED_OPEN

    pending = [p for p in pos_hint if (p.get("status") or "").lower() == "pending_entry"]
    if pending and not filled:
        return AUDIT_ORDER_UNFILLED
    if execution_outcome == OUTCOME_LIMIT_NOT_FILLED:
        return AUDIT_ORDER_UNFILLED
    if execution_outcome in (OUTCOME_POTENTIAL_MISSED, OUTCOME_ENGINE_DELAY):
        return AUDIT_MISSED_EXECUTABLE
    if execution_outcome in (
        OUTCOME_CONFIRMATION_INCOMPLETE,
        OUTCOME_NET_RR_BELOW_MIN,
        OUTCOME_INVALIDATED,
        OUTCOME_MARKET_DATA_GAP_STALE,
        OUTCOME_ORDER_REJECTED,
        OUTCOME_AMBIGUOUS_CHRONOLOGY,
    ):
        return AUDIT_POTENTIAL_NOT_EXECUTABLE
    if timeline.get("price_reached_tp_without_entry"):
        return AUDIT_DIRECTION_CORRECT
    if pipe.get("full_confirmations") and execution_outcome != OUTCOME_ORDER_CREATED:
        return AUDIT_MISSED_EXECUTABLE
    return AUDIT_DIRECTION_CORRECT


def _replay_unavailable_for_row(*, replay_status: str, execution_outcome: str) -> bool:
    if replay_status in (REPLAY_STATUS_INSUFFICIENT_HISTORY, REPLAY_STATUS_SEQUENCE_LIMITED):
        return True
    if execution_outcome == OUTCOME_AMBIGUOUS_CHRONOLOGY:
        return True
    return False


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
        OUTCOME_INTENTIONAL_WAIT,
        OUTCOME_TERMINAL_OTHER,
    )
    buckets: dict[str, list[dict[str, Any]]] = {k: [] for k in bucket_keys}

    replay_by_setup = replay_by_setup or {}
    rows: list[dict[str, Any]] = []
    for setup in setups:
        timeline = await build_setup_timeline(session, setup)
        replay_diff = replay_by_setup.get(setup.setup_id)
        execution_outcome = classify_execution_outcome(setup, timeline=timeline, replay_diff=replay_diff)
        replay_status = derive_replay_status(replay_diff)
        user_bucket = classify_user_audit_bucket(
            setup, timeline=timeline, execution_outcome=execution_outcome
        )
        pipe = (setup.objective_notes or {}).get("execution_pipeline") or {}
        entry = {
            "setup_id": setup.setup_id,
            "execution_outcome": execution_outcome,
            "user_audit_bucket": user_bucket,
            "replay_status": replay_status,
            "outcome": execution_outcome,
            "lifecycle_state": setup.lifecycle_state,
            "rejection_reason": setup.last_rejection_reason,
            "execution_pipeline": pipe,
            "timeline": timeline,
            "replay_diff": replay_diff,
            "chronology_advisories": chronology_advisories(replay_diff),
        }
        buckets.setdefault(execution_outcome, []).append(entry)
        rows.append(entry)

    summary = {k: len(v) for k, v in buckets.items()}
    summary["by_execution_outcome"] = dict(summary)
    summary["total_setups"] = len(rows)
    summary["by_replay_status"] = {}
    for row in rows:
        rs = row["replay_status"]
        summary["by_replay_status"][rs] = summary["by_replay_status"].get(rs, 0) + 1
    summary["potential_missed_count"] = sum(
        1
        for row in rows
        if row["execution_outcome"] == OUTCOME_POTENTIAL_MISSED and _replay_trusted(row.get("replay_diff"))
    )
    summary["insufficient_replay_history_count"] = summary["by_replay_status"].get(
        REPLAY_STATUS_INSUFFICIENT_HISTORY, 0
    )
    summary["replay_sequence_limited_count"] = summary["by_replay_status"].get(REPLAY_STATUS_SEQUENCE_LIMITED, 0)
    summary["replay_not_run_count"] = summary["by_replay_status"].get(REPLAY_STATUS_NOT_RUN, 0)
    summary["replay_unavailable_count"] = sum(
        1
        for row in rows
        if _replay_unavailable_for_row(
            replay_status=row["replay_status"],
            execution_outcome=row["execution_outcome"],
        )
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
