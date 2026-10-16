from __future__ import annotations

import pytest

from app.services.paper_opportunity_filters import heuristic_fill_score, heuristic_win_score


def test_heuristic_fill_blocks_far_poi_retest_limit():
    score = heuristic_fill_score(
        order_type="limit",
        distance_bps=90.0,
        mode_reason="poi_retest_limit",
        hours_left=1.0,
    )
    assert score < 0.5


def test_heuristic_fill_favors_near_market():
    score = heuristic_fill_score(
        order_type="market",
        distance_bps=12.0,
        mode_reason="confirmed_near_or_favorable_market",
        hours_left=8.0,
    )
    assert score >= 0.8


def test_heuristic_win_requires_ict_flags():
    base = {
        "liquidity_event_clear": True,
        "poi_reaction_clear": True,
        "structural_confirmation_clear": True,
        "logical_invalidation_clear": True,
    }
    assert heuristic_win_score(structured=base, net_rr=2.1, confirmations_complete=True) >= 0.75
    broken = {**base, "poi_reaction_clear": False, "structural_confirmation_clear": False}
    assert heuristic_win_score(structured=broken, net_rr=2.1, confirmations_complete=False) < 0.75


@pytest.mark.asyncio
async def test_dual_filter_rejects_low_fill_far_limit():
    from app.db import SessionLocal, init_db
    from app.models import AiProposedSetup
    from app.services.paper_opportunity_filters import evaluate_dual_trade_filters

    await init_db()
    async with SessionLocal() as session:
        setup = AiProposedSetup(
            setup_id="filt-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state="READY_LONG",
            entry_price=100.0,
            stop_loss=94.0,
            take_profit=115.0,
            structured_snapshot={
                "liquidity_event_clear": True,
                "poi_reaction_clear": True,
                "structural_confirmation_clear": True,
                "logical_invalidation_clear": True,
            },
        )
        session.add(setup)
        await session.flush()
        ok, reason, audit = await evaluate_dual_trade_filters(
            session,
            setup,
            structured=setup.structured_snapshot,
            order_type="limit",
            mode_diag={"distance_bps": 95.0, "mode_reason": "poi_retest_limit"},
            net_rr=2.2,
            live_price=101.0,
            confirmations_complete=True,
        )
        assert ok is False
        assert reason == "low_fill_probability"
        assert audit["calibration_status"] == "uncalibrated"
