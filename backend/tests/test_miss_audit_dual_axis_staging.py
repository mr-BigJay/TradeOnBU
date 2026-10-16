"""Staging regressions: execution outcome vs replay status (32c210c0, 29af5bb2)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.opportunity_miss_reasons import (
    OUTCOME_CONFIRMATION_INCOMPLETE,
    OUTCOME_NET_RR_BELOW_MIN,
    OUTCOME_POTENTIAL_MISSED,
    REPLAY_STATUS_SEQUENCE_LIMITED,
)
from app.domain.setup_lifecycle import (
    ICT_CONFIRMED_LONG,
    REASON_ICT_CONFIRMED_BLOCKED_RR,
    REASON_MISSING_CONFIRMATIONS,
    WAIT_LONG,
)
from app.services.opportunity_miss_report import (
    build_miss_audit_report,
    classify_execution_outcome,
    derive_replay_status,
)


def _setup_32c210c0() -> SimpleNamespace:
    return SimpleNamespace(
        setup_id="32c210c0-0000-4000-8000-000000000001",
        lifecycle_state=ICT_CONFIRMED_LONG,
        last_rejection_reason=REASON_ICT_CONFIRMED_BLOCKED_RR,
        objective_notes={
            "revision_history": [
                {
                    "kind": "conditional_revision",
                    "confirmation_sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
                    "prior_levels": {"entry_zone_low": 1.0, "entry_zone_high": 2.0},
                    "new_levels": {"entry_zone_low": 1.1, "entry_zone_high": 2.1},
                }
            ]
        },
        last_validation_result={"ok": False, "reason": REASON_ICT_CONFIRMED_BLOCKED_RR},
    )


def _setup_29af5bb2() -> SimpleNamespace:
    return SimpleNamespace(
        setup_id="29af5bb2-8bd4-48a9-a7cb-30fcdb32a551",
        lifecycle_state=WAIT_LONG,
        last_rejection_reason=REASON_MISSING_CONFIRMATIONS,
        objective_notes={
            "revision_history": [
                {
                    "kind": "conditional_revision",
                    "confirmation_sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
                }
            ]
        },
        last_validation_result={},
    )


def test_32c210c0_net_rr_preserved_when_replay_limited():
    setup = _setup_32c210c0()
    replay_diff = {
        "replay_status": REPLAY_STATUS_SEQUENCE_LIMITED,
        "replay_trusted": False,
        "missing_in_live": ["liquidity_sweep"],
    }
    assert (
        classify_execution_outcome(setup, timeline={"paper_orders": []}, replay_diff=replay_diff)
        == OUTCOME_NET_RR_BELOW_MIN
    )
    assert derive_replay_status(replay_diff) == REPLAY_STATUS_SEQUENCE_LIMITED
    assert (
        classify_execution_outcome(setup, timeline={"paper_orders": []}, replay_diff=replay_diff)
        != OUTCOME_POTENTIAL_MISSED
    )


def test_29af5bb2_wait_shows_confirmation_incomplete_with_limited_replay():
    setup = _setup_29af5bb2()
    replay_diff = {
        "replay_status": REPLAY_STATUS_SEQUENCE_LIMITED,
        "replay_trusted": False,
        "missing_in_live": ["liquidity_sweep"],
    }
    assert (
        classify_execution_outcome(setup, timeline={"paper_orders": []}, replay_diff=replay_diff)
        == OUTCOME_CONFIRMATION_INCOMPLETE
    )
    assert derive_replay_status(replay_diff) == REPLAY_STATUS_SEQUENCE_LIMITED


@pytest.mark.asyncio
async def test_report_summary_counts_trusted_miss_only(session):
    from app.db import SessionLocal, init_db
    from app.models import AiProposedSetup
    from datetime import datetime, timedelta, timezone

    await init_db()
    async with SessionLocal() as session:
        exp = datetime.now(timezone.utc) + timedelta(hours=8)
        for setup_id, lc, reason in (
            ("29af5bb2-8bd4-48a9-a7cb-30fcdb32a551", WAIT_LONG, REASON_MISSING_CONFIRMATIONS),
            ("32c210c0-0000-4000-8000-000000000002", ICT_CONFIRMED_LONG, REASON_ICT_CONFIRMED_BLOCKED_RR),
        ):
            session.add(
                AiProposedSetup(
                    setup_id=setup_id,
                    run_id=1,
                    symbol="BTCUSDT",
                    direction="LONG",
                    status="open",
                    lifecycle_state=lc,
                    monitoring_enabled=True,
                    last_rejection_reason=reason,
                    entry_price=100.0,
                    entry_zone_low=99.0,
                    entry_zone_high=101.0,
                    stop_loss=94.0,
                    take_profit=115.0,
                    invalidation_level=93.0,
                    expires_at=exp,
                    confirmation_state={"sequence": ["mss"], "confirmation_timestamps": {}},
                    objective_notes={"revision_history": [{"kind": "conditional_revision"}]},
                )
            )
        await session.commit()

        replay_by_setup = {
            "29af5bb2-8bd4-48a9-a7cb-30fcdb32a551": {
                "replay_status": REPLAY_STATUS_SEQUENCE_LIMITED,
                "replay_trusted": False,
                "missing_in_live": ["mss"],
            },
            "32c210c0-0000-4000-8000-000000000002": {
                "replay_status": REPLAY_STATUS_SEQUENCE_LIMITED,
                "replay_trusted": False,
            },
        }
        report = await build_miss_audit_report(
            session,
            symbol="BTCUSDT",
            limit=50,
            replay_by_setup=replay_by_setup,
        )
        assert report["summary"]["potential_missed_count"] == 0
        assert report["summary"]["replay_unavailable_count"] >= 2
        rows = {r["setup_id"]: r for r in report["rows"]}
        assert rows["29af5bb2-8bd4-48a9-a7cb-30fcdb32a551"]["execution_outcome"] == OUTCOME_CONFIRMATION_INCOMPLETE
        assert rows["29af5bb2-8bd4-48a9-a7cb-30fcdb32a551"]["replay_status"] == REPLAY_STATUS_SEQUENCE_LIMITED
        assert rows["32c210c0-0000-4000-8000-000000000002"]["execution_outcome"] == OUTCOME_NET_RR_BELOW_MIN
