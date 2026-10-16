"""Integration: production-shaped GPT JSON ↔ persistence contract."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.setup_lifecycle import WAIT_LONG
from app.models import AiProposedSetup
from app.services.ai_trader_instructions import ICT_TRADER_INSTRUCTIONS
from app.services.ai_trader_persistence_contract import validate_structured_for_persistence
from app.services.ai_setup_memory import create_setup_from_structured


def _production_conditional_wait_long() -> dict:
    """Fields aligned with ICT_TRADER_INSTRUCTIONS (not a hand-built setup row)."""
    return {
        "decision": "NO_TRADE",
        "final_ai_decision": "WAIT",
        "setup_mode": "conditional",
        "potential_direction": "LONG",
        "conditional_entry_zone_low": 99.0,
        "conditional_entry_zone_high": 101.0,
        "confirmation_required": "SSL sweep then 5m bullish MSS into POI retest",
        "confirmation_type": "sweep_mss_poi",
        "conditional_invalidation": "5m close below prior swing low at 93000",
        "structural_invalidation_level": 93.0,
        "stop_loss": 94.0,
        "take_profit": 115.0,
        "risk_reward": 2.1,
        "current_price": 100.0,
        "market_bias": "Bullish intraday pullback",
        "setup_timeframe": "intraday_5m_15m",
        "liquidity_event_clear": False,
        "poi_reaction_clear": False,
        "structural_confirmation_clear": False,
        "logical_invalidation_clear": True,
        "ict_liquidity": "SSL pool under 99.0",
        "ict_structure": "Awaiting sweep + MSS",
        "trade_reason": "Conditional long after liquidity run",
        "report_fa": "",
        "proposed_setup": {"new_setup": True, "thesis_fa": "Wait for ICT sequence"},
    }


def _production_immediate_long_monitored() -> dict:
    return {
        "decision": "LONG",
        "final_ai_decision": "LONG",
        "setup_mode": "immediate",
        "entry_price": 100.0,
        "entry_zone_low": 99.5,
        "entry_zone_high": 100.5,
        "stop_loss": 94.0,
        "take_profit": 115.0,
        "structural_invalidation_level": 93.0,
        "confirmation_type": "sweep_mss_poi",
        "liquidity_event_clear": True,
        "poi_reaction_clear": True,
        "structural_confirmation_clear": True,
        "logical_invalidation_clear": True,
        "proposed_setup": {"new_setup": True},
    }


def test_instructions_mention_execution_contract_fields():
    text = ICT_TRADER_INSTRUCTIONS
    assert "confirmation_type" in text
    assert "structural_invalidation_level" in text
    assert "sweep_mss_poi" in text


def test_production_conditional_json_passes_contract():
    data = _production_conditional_wait_long()
    assert validate_structured_for_persistence(data) is None


def test_production_conditional_missing_confirmation_type_rejected():
    data = _production_conditional_wait_long()
    data.pop("confirmation_type")
    assert validate_structured_for_persistence(data) == "missing_structured_confirmation_type"


@pytest.mark.asyncio
async def test_production_conditional_json_persists_via_pipeline():
    await init_db()
    data = _production_conditional_wait_long()
    async with SessionLocal() as session:
        row, meta = await create_setup_from_structured(session, run_id=42, symbol="BTCUSDT", structured=data)
        await session.commit()
        assert meta.get("created") is True
        assert row is not None
        loaded = (
            await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == row.setup_id))
        ).scalar_one()
        assert loaded.lifecycle_state == WAIT_LONG
        assert loaded.monitoring_enabled is True
        assert loaded.conditional_plan.get("confirmation_type") == "sweep_mss_poi"
        assert loaded.invalidation_level == 93.0


@pytest.mark.asyncio
async def test_immediate_monitored_json_persists_with_monitoring():
    from unittest.mock import AsyncMock, patch

    from app.domain.candles import Candle
    from app.services.market_data import KlineBundle

    await init_db()
    data = _production_immediate_long_monitored()
    empty = KlineBundle(
        closed=[Candle(time_ms=1, open=100, high=100.1, low=99.9, close=100, volume=1)],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    with patch("app.services.immediate_confirmation_verify.MarketDataService") as mcls:
        mcls.return_value.fetch_kline_bundle = AsyncMock(return_value=empty)
        async with SessionLocal() as session:
            row, meta = await create_setup_from_structured(session, run_id=43, symbol="BTCUSDT", structured=data)
            await session.commit()
        assert meta.get("created") is True
        assert row.monitoring_enabled is True
        assert row.lifecycle_state in (WAIT_LONG, "READY_LONG")
        seq = (row.confirmation_state or {}).get("sequence") or []
        assert seq[0] == "liquidity_sweep"


@pytest.mark.asyncio
async def test_immediate_long_without_monitor_fields_rejected():
    await init_db()
    data = {
        "decision": "LONG",
        "final_ai_decision": "LONG",
        "setup_mode": "immediate",
        "entry_price": 100.0,
        "stop_loss": 94.0,
        "take_profit": 115.0,
        "proposed_setup": {"new_setup": True},
    }
    async with SessionLocal() as session:
        row, meta = await create_setup_from_structured(session, run_id=44, symbol="BTCUSDT", structured=data)
        assert row is None
        assert meta["skipped"] is True
        assert meta["reason"] in ("immediate_missing_monitor_requirements", "missing_structured_confirmation_type")


@pytest.mark.asyncio
async def test_conditional_revision_updates_all_levels():
    await init_db()
    data = _production_conditional_wait_long()
    data2 = _production_conditional_wait_long()
    data2["take_profit"] = 118.0
    data2["stop_loss"] = 93.5
    data2["structural_invalidation_level"] = 92.5
    async with SessionLocal() as session:
        row, _ = await create_setup_from_structured(session, run_id=1, symbol="BTCUSDT", structured=data)
        await session.commit()
        sid = row.setup_id
        row2, meta = await create_setup_from_structured(session, run_id=2, symbol="BTCUSDT", structured=data2)
        await session.commit()
        assert meta.get("updated") is True
        assert row2.setup_id == sid
        assert row2.take_profit == 118.0
        assert row2.stop_loss == 93.5
        assert row2.invalidation_level == 92.5
        notes = row2.objective_notes or {}
        assert notes.get("revision_history")


@pytest.mark.asyncio
async def test_conditional_revision_blocked_when_pending_order():
    await init_db()
    from app.models import SimPosition

    data = _production_conditional_wait_long()
    async with SessionLocal() as session:
        row, _ = await create_setup_from_structured(session, run_id=1, symbol="BTCUSDT", structured=data)
        session.add(
            SimPosition(
                public_id="pending-1",
                symbol="BTCUSDT",
                direction="LONG",
                status="pending_entry",
                order_type="limit",
                margin_usdt=10,
                leverage=5,
                limit_entry_price=100,
                stop_loss=94,
                take_profit=115,
                ai_setup_id=row.setup_id,
            )
        )
        await session.commit()
        _, meta = await create_setup_from_structured(session, run_id=2, symbol="BTCUSDT", structured=data)
        assert meta.get("skipped") is True
        assert meta.get("reason") == "setup_locked_active_position"
