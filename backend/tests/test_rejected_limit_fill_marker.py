"""The fill monitor must not invent an entry when the fill engine rejects it."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.domain.candles import Candle
from app.models import SimPosition
from app.services.market_data import KlineBundle
from app.services.position.position_monitor import SimPositionMonitorService


@pytest.mark.asyncio
async def test_rejected_limit_fill_does_not_record_entry_bar():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    bar_ms = ((now_ms // 60_000) - 2) * 60_000
    bar = Candle(time_ms=bar_ms, open=99, high=101, low=98, close=100, volume=1)
    bundle = KlineBundle(
        closed=[bar], forming=None, gaps_detected=[], gaps_remaining=[],
        data_complete=True, interval="1m",
    )
    async with SessionLocal() as session:
        pos = SimPosition(
            public_id="rejected-limit-fill-check",
            symbol="BTCUSDT", direction="LONG", status="pending_entry",
            order_type="limit", margin_usdt=50.0, leverage=10,
            limit_entry_price=100.0, stop_loss=95.0, take_profit=110.0,
            monitor_from_ms=bar_ms,
            extra={},
        )
        session.add(pos)
        await session.commit()

    monitor = SimPositionMonitorService()
    with patch.object(monitor.market, "fetch_kline_bundle", new=AsyncMock(return_value=bundle)):
        with patch.object(monitor.positions, "_fill_entry", new=AsyncMock(return_value=False)) as attempted:
            async with SessionLocal() as session:
                await monitor.monitor_session(session)
                persisted = (await session.execute(select(SimPosition).where(SimPosition.public_id == "rejected-limit-fill-check"))).scalar_one()
                assert attempted.await_count == 1
                assert persisted.status == "pending_entry"
                assert "entry_fill_bar_ms" not in (persisted.extra or {})
