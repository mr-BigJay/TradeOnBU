from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.candles import Candle
from app.services.market_data import KlineBundle, MarketDataService
from app.services.monitor_kline_fetch import fetch_monitor_5m_bundle


pytestmark = pytest.mark.no_monitor_fetch_delegate


@pytest.mark.asyncio
async def test_fetch_monitor_delegates_to_historical_when_backfill_needed():
    now_ms = int(datetime(2026, 10, 16, tzinfo=timezone.utc).timestamp() * 1000)
    step = 300_000
    setup = SimpleNamespace(
        setup_id="laggy",
        created_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
        monitor_from_ms=now_ms - 400 * step,
        monitor_cursor_ms=now_ms - 350 * step,
    )
    bundle = KlineBundle(
        closed=[Candle(time_ms=now_ms - step, open=1, high=1, low=1, close=1, volume=1)],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    market = MarketDataService()
    market.fetch_historical_closed = AsyncMock(return_value=bundle)  # type: ignore[method-assign]
    market.fetch_kline_bundle = AsyncMock(return_value=bundle)  # type: ignore[method-assign]

    out, meta = await fetch_monitor_5m_bundle(market, "BTCUSDT", [setup], now_ms=now_ms)
    assert meta["fetch_mode"] == "historical_paginated"
    market.fetch_historical_closed.assert_awaited()
    assert out.data_complete is True
