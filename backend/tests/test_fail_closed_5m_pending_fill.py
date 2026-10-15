"""AI-linked pending limits must not fill when 5m data is unavailable."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.candles import Candle
from app.domain.setup_lifecycle import PENDING_ENTRY
from app.models import AiProposedSetup, SimPosition, SimPositionSettings
from app.services.market_data import KlineBundle
from app.services.position.position_monitor import SimPositionMonitorService
from app.worker import BotWorker

INTERVAL_1M = 60_000


@pytest.mark.asyncio
async def test_sim_blocks_fill_when_5m_incomplete_and_opportunity_tick_skipped():
    await init_db()
    now = datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc)
    order_ms = int(now.timestamp() * 1000)
    fill_bar = Candle(
        time_ms=order_ms + INTERVAL_1M,
        open=99.5,
        high=100.5,
        low=99.0,
        close=100.2,
        volume=1.0,
    )

    async with SessionLocal() as session:
        settings = (await session.execute(select(SimPositionSettings))).scalar_one_or_none()
        if not settings:
            session.add(SimPositionSettings(id=1))
        setup = AiProposedSetup(
            setup_id="fail-closed-5m",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=PENDING_ENTRY,
            monitoring_enabled=True,
            monitor_from_ms=order_ms,
            monitor_cursor_ms=order_ms - INTERVAL_1M,
            entry_price=100.0,
            stop_loss=94.0,
            take_profit=115.0,
            invalidation_level=93.0,
            expires_at=now + timedelta(hours=6),
        )
        session.add(setup)
        session.add(
            SimPosition(
                public_id="pos-fail-closed",
                symbol="BTCUSDT",
                direction="LONG",
                status="pending_entry",
                order_type="limit",
                margin_usdt=50.0,
                leverage=10,
                limit_entry_price=100.0,
                stop_loss=94.0,
                take_profit=115.0,
                ai_setup_id="fail-closed-5m",
                monitor_from_ms=order_ms,
                last_processed_candle_ms=order_ms,
                extra={"auto_paper": True},
            )
        )
        await session.commit()

    bundle_1m = KlineBundle(
        closed=[fill_bar],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="1m",
    )
    bundle_5m_bad = KlineBundle(
        closed=[],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=False,
        interval="5m",
    )

    monitor_at = datetime.fromtimestamp((order_ms + INTERVAL_1M * 2) / 1000, tz=timezone.utc)
    worker = BotWorker()

    async def _fetch(symbol: str, interval: str, limit: int):
        if interval == "1m":
            return bundle_1m
        if interval == "5m":
            return bundle_5m_bad
        return bundle_5m_bad

    with patch("app.worker.try_acquire_worker_lock", AsyncMock(return_value=True)):
        with patch("app.worker.release_worker_lock", AsyncMock()):
            with patch.object(worker, "monitor") as mock_sig_mon:
                mock_sig_mon.monitor_open_signals = AsyncMock(return_value={})
                with patch(
                    "app.services.opportunity_monitor.run_opportunity_monitor_tick",
                    AsyncMock(return_value={"skipped": True, "reason": "monitor_lock_held"}),
                ):
                    with patch(
                        "app.services.market_data.MarketDataService.fetch_kline_bundle",
                        AsyncMock(side_effect=_fetch),
                    ):
                        with patch("app.services.opportunity_monitor._utc_now", return_value=monitor_at):
                            with patch("app.services.paper_auto_trading._utc_now", return_value=monitor_at):
                                await worker.run_cycle()

    async with SessionLocal() as session:
        pos = (
            await session.execute(select(SimPosition).where(SimPosition.public_id == "pos-fail-closed"))
        ).scalar_one()
        setup = (
            await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "fail-closed-5m"))
        ).scalar_one()
        assert pos.status == "pending_entry"
        assert pos.status != "open"
        notes = setup.objective_notes or {}
        assert notes.get("pending_fill_suspended") is True
        assert notes.get("pending_fill_block_reason") == "missing_or_incomplete_5m_data"
