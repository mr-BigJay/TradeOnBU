"""Regression: stale 5m data must not cancel pending paper orders."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.candles import Candle
from app.domain.setup_lifecycle import PENDING_ENTRY, READY_LONG
from app.models import AiProposedSetup, SimPosition, SimPositionSettings
from app.services.market_data import KlineBundle
from app.services.opportunity_monitor import OpportunityMonitorService
from app.services.position import wallet_service as wallet
from app.services.position.position_monitor import SimPositionMonitorService


@pytest.mark.asyncio
async def test_stale_5m_outage_then_recovery_pending_not_cancelled_and_can_fill():
    await init_db()
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=50_000.0)
        settings = (await session.execute(select(SimPositionSettings))).scalar_one_or_none()
        if not settings:
            session.add(SimPositionSettings(id=1))
        settings = (await session.execute(select(SimPositionSettings))).scalar_one()
        settings.initial_balance_set = True
        settings.auto_open_paper_positions = True

        setup = AiProposedSetup(
            setup_id="stale-pending-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=PENDING_ENTRY,
            monitoring_enabled=True,
            entry_price=100.0,
            entry_zone_low=99.0,
            entry_zone_high=101.0,
            stop_loss=94.0,
            take_profit=115.0,
            invalidation_level=93.0,
            expires_at=now + timedelta(hours=24),
            last_validation_result={"ok": True, "source": "promoted_ready"},
            structured_snapshot={
                "decision": "LONG",
                "liquidity_event_clear": True,
                "poi_reaction_clear": True,
                "structural_confirmation_clear": True,
                "logical_invalidation_clear": True,
            },
        )
        session.add(setup)
        order_ms = int(now.timestamp() * 1000)
        session.add(
            SimPosition(
                public_id="pos-stale-1",
                symbol="BTCUSDT",
                direction="LONG",
                status="pending_entry",
                order_type="limit",
                margin_usdt=50,
                leverage=10,
                limit_entry_price=100.0,
                stop_loss=94.0,
                take_profit=115.0,
                ai_setup_id=setup.setup_id,
                monitor_from_ms=order_ms,
                entry_is_maker=True,
                extra={"auto_paper": True},
            )
        )
        await session.commit()

    stale_bundle = KlineBundle(
        closed=[],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=False,
        interval="5m",
    )
    mon = OpportunityMonitorService()
    async with SessionLocal() as session:
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=stale_bundle)):
            with patch("app.services.opportunity_monitor._utc_now", return_value=now):
                await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()
        setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "stale-pending-1"))).scalar_one()
        pos = (await session.execute(select(SimPosition))).scalar_one()
        assert setup.lifecycle_state == PENDING_ENTRY
        assert (setup.last_validation_result or {}).get("ok") is True
        assert (setup.objective_notes or {}).get("market_data_health", {}).get("ok") is False
        assert pos.status == "pending_entry"

    fresh_bar = Candle(time_ms=order_ms + 300_000, open=100, high=101, low=99, close=100.2, volume=1)
    fresh_bundle = KlineBundle(
        closed=[fresh_bar],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    async with SessionLocal() as session:
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=fresh_bundle)):
            with patch("app.services.opportunity_monitor._utc_now", return_value=now + timedelta(minutes=6)):
                await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()

    pre_bar = Candle(time_ms=order_ms, open=100, high=100.4, low=100.01, close=100.2, volume=1)
    fill_bar = Candle(
        time_ms=order_ms + 60_000, open=100, high=100.5, low=99.5, close=100, volume=1
    )
    bundle_1m = KlineBundle(
        closed=[pre_bar, fill_bar],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="1m",
    )
    async with SessionLocal() as session:
        pos = (await session.execute(select(SimPosition))).scalar_one()
        pos.last_processed_candle_ms = 0
        await session.commit()
        sim = SimPositionMonitorService()
        with patch.object(sim.market, "fetch_kline_bundle", AsyncMock(return_value=bundle_1m)):
            await sim.monitor_session(session)
        await session.commit()
        pos = (await session.execute(select(SimPosition))).scalar_one()
        assert pos.status == "open", pos.extra
