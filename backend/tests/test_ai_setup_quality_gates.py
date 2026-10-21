"""Quality gates: regime, contrarian evidence, repeat-direction memory, stale market context."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.setup_lifecycle import WAIT_LONG, WAIT_SHORT
from app.models import AiProposedSetup, SimPosition
from app.services.ai_setup_memory import create_setup_from_structured
from app.services.ai_setup_quality_gates import validate_setup_quality_gates
from tests.test_ai_trader_persistence_contract import (
    _production_conditional_wait_long,
    _production_immediate_long_monitored,
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


def _contrarian_evidence_ok() -> dict:
    return {
        "liquidity_sweep": True,
        "structure_shift_mss_or_bos": True,
        "poi_reaction": True,
    }


def _independence_block() -> dict:
    return {
        "independent_from_prior_setup": True,
        "prior_setup_reference_fa": "شورت قبلی در 84000 با SL در 84500",
        "new_evidence_summary_fa": "sweep جدید SSL در 84520 و MSS نزولی تازه در 5m",
        "what_invalidated_prior_fa": "شکست ساختار صعودی کوتاه‌مدت و جمع‌آوری BSL",
    }


@pytest.mark.asyncio
async def test_bullish_regime_blocks_counter_short_without_ict_evidence():
    await init_db()
    data = _conditional_short_against_bullish()
    async with SessionLocal() as session:
        err, _audit = await validate_setup_quality_gates(session, symbol="BTCUSDT", structured=data)
        assert err == "contrarian_without_validated_ict_evidence"
        row, meta = await create_setup_from_structured(session, run_id=1, symbol="BTCUSDT", structured=data)
        await session.commit()
        assert row is None
        assert meta["reason"] == "contrarian_without_validated_ict_evidence"


@pytest.mark.asyncio
async def test_repeat_short_after_sl_requires_independence():
    await init_db()
    async with SessionLocal() as session:
        session.add(
            SimPosition(
                public_id="sim-loss-1",
                symbol="BTCUSDT",
                direction="SHORT",
                status="closed",
                margin_usdt=50.0,
                leverage=10,
                entry_price=84000.0,
                stop_loss=84500.0,
                take_profit=82000.0,
                fill_price=84000.0,
                exit_price=84500.0,
                net_pnl_usdt=-12.0,
                close_reason="stop_loss",
                updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
            )
        )
        await session.commit()
        data = _conditional_short_against_bullish(
            contrarian_ict_evidence=_contrarian_evidence_ok(),
            market_regime="range",
            trade_against_dominant_trend=False,
        )
        err, _ = await validate_setup_quality_gates(session, symbol="BTCUSDT", structured=data)
        assert err == "repeat_direction_after_loss_requires_independence"


@pytest.mark.asyncio
async def test_counter_trend_allowed_with_valid_contrarian_evidence():
    await init_db()
    data = _conditional_short_against_bullish(contrarian_ict_evidence=_contrarian_evidence_ok())
    async with SessionLocal() as session:
        err, audit = await validate_setup_quality_gates(session, symbol="BTCUSDT", structured=data)
        assert err is None
        assert audit["trade_against_dominant_trend"] is True
        row, meta = await create_setup_from_structured(session, run_id=2, symbol="BTCUSDT", structured=data)
        await session.commit()
        assert meta.get("created") is True
        assert row is not None
        assert row.lifecycle_state == WAIT_SHORT


@pytest.mark.asyncio
async def test_conditional_wait_keeps_false_initial_flags_when_gates_pass():
    await init_db()
    data = _production_conditional_wait_long()
    async with SessionLocal() as session:
        row, meta = await create_setup_from_structured(session, run_id=3, symbol="BTCUSDT", structured=data)
        await session.commit()
        assert meta.get("created") is True
        assert row.lifecycle_state == WAIT_LONG
        snap = row.structured_snapshot or {}
        assert snap.get("poi_reaction_clear") is False
        assert (row.objective_notes or {}).get("setup_quality_audit")


@pytest.mark.asyncio
async def test_stale_market_context_blocks_high_confidence_regime_claim():
    await init_db()
    data = _production_immediate_long_monitored()
    data["market_regime"] = "bullish"
    data["market_regime_confidence"] = "high"
    async with SessionLocal() as session:
        err, _ = await validate_setup_quality_gates(
            session,
            symbol="BTCUSDT",
            structured=data,
            market_intelligence={"status": "unavailable", "reason": "no_public_snapshot"},
        )
        assert err == "market_context_stale_for_regime_claim"


@pytest.mark.asyncio
async def test_immediate_long_requires_four_ai_gates():
    await init_db()
    data = _production_immediate_long_monitored()
    data["poi_reaction_clear"] = False
    async with SessionLocal() as session:
        err, _ = await validate_setup_quality_gates(session, symbol="BTCUSDT", structured=data)
        assert err == "immediate_four_ict_gates_incomplete"


@pytest.mark.asyncio
async def test_setup_lineage_revision_preserves_setup_id_not_run_id():
    await init_db()
    data = _production_conditional_wait_long()
    async with SessionLocal() as session:
        row1, meta1 = await create_setup_from_structured(session, run_id=10, symbol="BTCUSDT", structured=data)
        await session.commit()
        setup_id = row1.setup_id
        data2 = dict(data)
        data2["confirmation_required"] = "بازبینی تأیید POI پس از run جدید"
        row2, meta2 = await create_setup_from_structured(session, run_id=11, symbol="BTCUSDT", structured=data2)
        await session.commit()
        assert meta2.get("reason") == "conditional_revision"
        assert row2.setup_id == setup_id
        loaded = (
            await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == setup_id))
        ).scalar_one()
        assert loaded.run_id == 11
        notes = loaded.objective_notes or {}
        assert notes.get("setup_quality_audit") or (loaded.structured_snapshot or {}).get("setup_quality_audit")


@pytest.mark.asyncio
async def test_repeat_short_allowed_with_independence_after_loss():
    await init_db()
    async with SessionLocal() as session:
        session.add(
            SimPosition(
                public_id="sim-loss-2",
                symbol="BTCUSDT",
                direction="SHORT",
                status="closed",
                margin_usdt=50.0,
                leverage=10,
                entry_price=84000.0,
                stop_loss=84500.0,
                take_profit=82000.0,
                fill_price=84000.0,
                exit_price=84500.0,
                net_pnl_usdt=-8.0,
                close_reason="stop_loss",
                updated_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        )
        await session.commit()
        data = _conditional_short_against_bullish(
            contrarian_ict_evidence=_contrarian_evidence_ok(),
            setup_independence=_independence_block(),
            proposed_setup={"new_setup": True, "distinct_new_setup": True},
        )
        err, _ = await validate_setup_quality_gates(session, symbol="BTCUSDT", structured=data)
        assert err is None


@pytest.mark.asyncio
async def test_immediate_persists_with_mocked_objective_verify():
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
            row, meta = await create_setup_from_structured(session, run_id=99, symbol="BTCUSDT", structured=data)
            await session.commit()
            assert meta.get("created") is True or meta.get("skipped") is False
            assert row is not None
