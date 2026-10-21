"""Quality gates: regime, SL status, structural repeat evidence, revisions, pre-order checks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.candles import Candle
from app.domain.setup_lifecycle import READY_SHORT, WAIT_LONG, WAIT_SHORT
from app.models import AiProposedSetup, SimPosition
from app.services.ai_setup_memory import create_setup_from_structured
from app.services.ai_setup_quality_gates import (
    _recent_losing_sim_same_direction,
    confirmations_strictly_after_ms,
    is_sim_summary_loss,
    objective_sequence_evidence_after_ms,
    sim_position_failure_ms,
    validate_ready_setup_before_paper_order,
    validate_setup_quality_gates,
)
from app.services.market_data import KlineBundle
from tests.test_ai_trader_persistence_contract import (
    _production_conditional_wait_long,
    _production_immediate_long_monitored,
)


def _loss_position(*, public_id: str, status: str = "sl_hit") -> SimPosition:
    return SimPosition(
        public_id=public_id,
        symbol="BTCUSDT",
        direction="SHORT",
        status=status,
        margin_usdt=50.0,
        leverage=10,
        entry_price=84000.0,
        stop_loss=84500.0,
        take_profit=82000.0,
        fill_price=84000.0,
        exit_price=84500.0,
        net_pnl_usdt=-15.0,
        close_reason="stop_loss" if status == "sl_hit" else "closed",
        closed_at=datetime.now(timezone.utc) - timedelta(hours=3),
        updated_at=datetime.now(timezone.utc) - timedelta(hours=3),
    )


def _conditional_short_against_bullish(**extra) -> dict:
    base = {
        "decision": "NO_TRADE",
        "final_ai_decision": "WAIT",
        "setup_mode": "conditional",
        "potential_direction": "SHORT",
        "market_regime": "bullish",
        "trade_against_dominant_trend": True,
        "conditional_entry_zone_low": 84400.0,
        "conditional_entry_zone_high": 84600.0,
        "confirmation_required": "نیاز به sweep نزولی و MSS نزولی",
        "confirmation_type": "sweep_mss_poi",
        "structural_invalidation_level": 85200.0,
        "conditional_invalidation": "شکست سطح ابطال ساختاری",
        "stop_loss": 85200.0,
        "take_profit": 82000.0,
        "risk_reward": 2.2,
        "liquidity_event_clear": False,
        "poi_reaction_clear": False,
        "structural_confirmation_clear": False,
        "logical_invalidation_clear": True,
        "proposed_setup": {"new_setup": True},
    }
    base.update(extra)
    return base


@pytest.mark.parametrize("public_id", ["paper-pos-8", "paper-pos-9", "paper-pos-10"])
def test_is_sim_summary_loss_treats_sl_hit_like_production(public_id: str):
    row = {
        "sim_position_id": public_id,
        "status": "sl_hit",
        "net_pnl_usdt": -10.0,
        "close_reason": "stop_loss",
    }
    assert is_sim_summary_loss(row) is True


@pytest.mark.asyncio
async def test_recent_loss_query_includes_sl_hit_not_only_closed():
    await init_db()
    async with SessionLocal() as session:
        session.add(_loss_position(public_id="paper-pos-10", status="sl_hit"))
        await session.commit()
        loss = await _recent_losing_sim_same_direction(session, symbol="BTCUSDT", direction="SHORT")
        assert loss is not None
        assert loss.public_id == "paper-pos-10"
        assert loss.status == "sl_hit"


@pytest.mark.asyncio
async def test_conditional_wait_short_in_bullish_allowed_without_upfront_ict_booleans():
    await init_db()
    async with SessionLocal() as session:
        session.add(_loss_position(public_id="paper-pos-9"))
        await session.commit()
    data = _conditional_short_against_bullish()
    async with SessionLocal() as session:
        err, audit = await validate_setup_quality_gates(session, symbol="BTCUSDT", structured=data)
        assert err is None
        assert any(c.get("deferred_to_monitor") for c in audit.get("checks", []) if c.get("gate") == "repeat_direction_after_loss")
        row, meta = await create_setup_from_structured(session, run_id=1, symbol="BTCUSDT", structured=data)
        await session.commit()
        assert meta.get("created") is True
        assert row.lifecycle_state == WAIT_SHORT


@pytest.mark.asyncio
async def test_immediate_short_after_sl_hit_requires_objective_structure_on_closed_5m():
    await init_db()
    async with SessionLocal() as session:
        session.add(_loss_position(public_id="paper-pos-8"))
        await session.commit()
    data = _production_immediate_long_monitored()
    data.update(
        {
            "decision": "SHORT",
            "final_ai_decision": "SHORT",
            "entry_price": 84000.0,
            "entry_zone_low": 83900.0,
            "entry_zone_high": 84100.0,
            "stop_loss": 84500.0,
            "take_profit": 82000.0,
            "structural_invalidation_level": 85200.0,
        }
    )
    empty = KlineBundle(
        closed=[Candle(time_ms=1_700_000_000_000, open=100, high=100.1, low=99.9, close=100, volume=1.0)],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    with patch("app.services.market_data.MarketDataService") as mcls:
        mcls.return_value.fetch_kline_bundle = AsyncMock(return_value=empty)
        async with SessionLocal() as session:
            err, _ = await validate_setup_quality_gates(session, symbol="BTCUSDT", structured=data)
            assert err == "repeat_direction_no_objective_structure_since_loss"


@pytest.mark.asyncio
async def test_conditional_revision_runs_quality_gates_and_records_audit():
    await init_db()
    data = _production_conditional_wait_long()
    async with SessionLocal() as session:
        row1, _ = await create_setup_from_structured(session, run_id=10, symbol="BTCUSDT", structured=data)
        await session.commit()
        setup_id = row1.setup_id
        data2 = dict(data)
        data2["confirmation_required"] = "بازبینی با کنترل کیفیت"
        row2, meta = await create_setup_from_structured(session, run_id=11, symbol="BTCUSDT", structured=data2)
        await session.commit()
        assert meta.get("reason") == "conditional_revision"
        assert row2.setup_id == setup_id
        notes = row2.objective_notes or {}
        assert notes.get("setup_quality_audit")
        assert notes.get("quality_audit_history")


@pytest.mark.asyncio
async def test_conditional_wait_keeps_false_initial_flags():
    await init_db()
    data = _production_conditional_wait_long()
    async with SessionLocal() as session:
        row, meta = await create_setup_from_structured(session, run_id=3, symbol="BTCUSDT", structured=data)
        await session.commit()
        assert meta.get("created") is True
        assert row.lifecycle_state == WAIT_LONG
        assert (row.structured_snapshot or {}).get("poi_reaction_clear") is False


@pytest.mark.asyncio
async def test_pre_order_rejects_confirmations_stamped_before_prior_sl_hit():
    await init_db()
    async with SessionLocal() as session:
        loss_row = _loss_position(public_id="paper-pos-8")
        session.add(loss_row)
        await session.flush()
        failure_ms = sim_position_failure_ms(loss_row)
        setup = AiProposedSetup(
            setup_id="pre-q-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="SHORT",
            status="open",
            lifecycle_state=READY_SHORT,
            entry_price=84000.0,
            stop_loss=84500.0,
            take_profit=82000.0,
            invalidation_level=85200.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            confirmation_state={
                "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "confirmation_timestamps": {
                    "liquidity_sweep": failure_ms - 600_000,
                    "mss": failure_ms - 300_000,
                    "entry_zone_touch": failure_ms - 60_000,
                },
            },
        )
        session.add(setup)
        await session.commit()
        ok, reason = await validate_ready_setup_before_paper_order(
            session,
            setup,
            latest_closed_5m=None,
            now_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
        )
        assert ok is False
        assert reason == "confirmations_not_after_prior_same_direction_loss"


@pytest.mark.asyncio
async def test_pre_order_blocks_stacked_same_direction_sim_position():
    await init_db()
    async with SessionLocal() as session:
        session.add(
            SimPosition(
                public_id="other-short-open",
                symbol="BTCUSDT",
                direction="SHORT",
                status="open",
                margin_usdt=50.0,
                leverage=10,
                entry_price=84100.0,
                stop_loss=84600.0,
                take_profit=82100.0,
                ai_setup_id="other-setup",
            )
        )
        setup = AiProposedSetup(
            setup_id="pre-q-2",
            run_id=2,
            symbol="BTCUSDT",
            direction="SHORT",
            status="open",
            lifecycle_state=READY_SHORT,
            entry_price=84000.0,
            stop_loss=84500.0,
            take_profit=82000.0,
            invalidation_level=85200.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            confirmation_state={
                "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "confirmation_timestamps": {
                    "liquidity_sweep": 1_700_000_000_000,
                    "mss": 1_700_000_300_000,
                    "entry_zone_touch": 1_700_000_600_000,
                },
            },
        )
        session.add(setup)
        await session.commit()
        ok, reason = await validate_ready_setup_before_paper_order(
            session,
            setup,
            latest_closed_5m=None,
            now_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
        )
        assert ok is False
        assert reason == "stacked_same_direction_paper_position"


def test_confirmations_strictly_after_ms_requires_increasing_after_failure():
    setup = AiProposedSetup(
        setup_id="x",
        run_id=1,
        symbol="BTCUSDT",
        direction="SHORT",
        status="open",
        confirmation_state={
            "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "confirmation_timestamps": {
                "liquidity_sweep": 1000,
                "mss": 2000,
                "entry_zone_touch": 3000,
            },
        },
    )
    assert confirmations_strictly_after_ms(setup, 500)[0] is True
    assert confirmations_strictly_after_ms(setup, 2500)[0] is False


def test_objective_sequence_evidence_false_when_tokens_missing_on_bars():
    t0 = 1_000_000
    bars = [
        Candle(time_ms=t0, open=100, high=100.2, low=99.8, close=100, volume=1),
    ]
    ok, hits = objective_sequence_evidence_after_ms(
        direction="LONG",
        sequence=["liquidity_sweep", "mss", "entry_zone_touch"],
        closed_5m=bars,
        after_ms=t0 - 1,
        zone_lo=99.0,
        zone_hi=101.0,
    )
    assert ok is False
    assert len(hits) < 3
