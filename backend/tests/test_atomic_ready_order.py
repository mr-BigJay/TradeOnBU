from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal, init_db
from app.domain.candles import Candle
from app.domain.setup_lifecycle import PENDING_ENTRY, READY_LONG
from app.models import AiProposedSetup, SimPosition, SimPositionSettings
from app.services.market_data import KlineBundle
from app.services.opportunity_monitor import OpportunityMonitorService
from app.services.position import wallet_service as wallet


@pytest.mark.asyncio
async def test_order_failure_leaves_ready_without_orphan_pending_or_margin():
    await init_db()
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=10_000.0)
        settings = (await session.execute(select(SimPositionSettings))).scalar_one_or_none()
        if not settings:
            session.add(SimPositionSettings(id=1))
        settings = (await session.execute(select(SimPositionSettings))).scalar_one()
        settings.initial_balance_set = True
        settings.auto_open_paper_positions = True

        setup = AiProposedSetup(
            setup_id="atomic-ready",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=READY_LONG,
            monitoring_enabled=True,
            monitor_from_ms=0,
            monitor_cursor_ms=0,
            entry_price=100.0,
            entry_zone_low=99.0,
            entry_zone_high=101.0,
            stop_loss=94.0,
            take_profit=115.0,
            invalidation_level=93.0,
            expires_at=now + timedelta(hours=12),
            last_validation_result={"ok": True, "source": "test"},
            structured_snapshot={"decision": "LONG"},
        )
        session.add(setup)
        await session.commit()

    bundle = KlineBundle(
        closed=[Candle(time_ms=300_000, open=100, high=100.5, low=99.5, close=100.2, volume=1)],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    mon = OpportunityMonitorService()

    async def _boom(*args, **kwargs):
        raise RuntimeError("injected_order_failure")

    async with SessionLocal() as session:
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            with patch("app.services.opportunity_monitor._utc_now", return_value=now + timedelta(minutes=6)):
                with patch(
                    "app.services.opportunity_monitor.maybe_auto_open_paper_position",
                    side_effect=_boom,
                ):
                    await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()
        setup = (
            await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "atomic-ready"))
        ).scalar_one()
        count = (await session.execute(select(func.count()).select_from(SimPosition))).scalar_one()
        assert setup.lifecycle_state == READY_LONG
        assert setup.lifecycle_state != PENDING_ENTRY
        assert count == 0
        summary = await wallet.wallet_summary(session)
        assert summary["margin_in_use"] == 0.0


@pytest.mark.asyncio
async def test_audit_failure_after_order_create_rolls_back_position_and_margin():
    """Failure after create_order + margin reserve must not commit partial state."""
    await init_db()
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=10_000.0)
        settings = (await session.execute(select(SimPositionSettings))).scalar_one_or_none()
        if not settings:
            session.add(SimPositionSettings(id=1))
        settings = (await session.execute(select(SimPositionSettings))).scalar_one()
        settings.initial_balance_set = True
        settings.auto_open_paper_positions = True
        settings.default_margin_usdt = 50.0
        settings.default_leverage = 10

        setup = AiProposedSetup(
            setup_id="atomic-audit-fail",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=READY_LONG,
            monitoring_enabled=True,
            monitor_from_ms=300_000,
            monitor_cursor_ms=299_999,
            entry_price=100.0,
            entry_zone_low=99.0,
            entry_zone_high=101.0,
            stop_loss=94.0,
            take_profit=115.0,
            invalidation_level=93.0,
            expires_at=now + timedelta(hours=12),
            last_validation_result={"ok": True, "net_rr": 2.5},
            structured_snapshot={"decision": "LONG", "final_ai_decision": "LONG"},
        )
        session.add(setup)
        await session.commit()

    bundle = KlineBundle(
        closed=[Candle(time_ms=300_000, open=100, high=100.5, low=99.5, close=100.2, volume=1.0)],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    mon = OpportunityMonitorService()
    monitor_at = now + timedelta(minutes=6)

    async with SessionLocal() as session:
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            with patch("app.services.opportunity_monitor._utc_now", return_value=monitor_at):
                with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 5, 600)):
                    with patch(
                        "app.services.opportunity_monitor.log_setup_event",
                        side_effect=RuntimeError("injected_post_create_audit_failure"),
                    ):
                        await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()
        setup = (
            await session.execute(
                select(AiProposedSetup).where(AiProposedSetup.setup_id == "atomic-audit-fail")
            )
        ).scalar_one()
        pos_count = (await session.execute(select(func.count()).select_from(SimPosition))).scalar_one()
        assert setup.lifecycle_state == READY_LONG
        assert setup.lifecycle_state != PENDING_ENTRY
        assert pos_count == 0
        summary = await wallet.wallet_summary(session)
        assert summary["margin_in_use"] == 0.0
