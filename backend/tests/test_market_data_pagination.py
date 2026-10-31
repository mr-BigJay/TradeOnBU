"""Regression tests for Bitunix paging used by BALANCED and paper monitor."""

from __future__ import annotations

import pytest

from app.domain.candles import Candle
from app.services.market_data import MarketDataService

# conftest's offline guard stubs the entire bundle method; retain the real
# function at collection time and restore it explicitly, while mocking HTTP.
_REAL_FETCH_KLINE_BUNDLE = MarketDataService.fetch_kline_bundle


@pytest.mark.asyncio
async def test_paginated_1m_history_never_exceeds_exchange_page_limit(monkeypatch):
    monkeypatch.setattr(MarketDataService, "fetch_kline_bundle", _REAL_FETCH_KLINE_BUNDLE)
    svc = MarketDataService()
    step = 60_000
    start = 1_800_000_000_000
    candles = [
        Candle(time_ms=start + i * step, open=100, high=101, low=99, close=100, volume=1)
        for i in range(620)
    ]
    calls = []

    async def fake_window(symbol, interval, limit=200, end_time_ms=None):
        assert symbol == "BTCUSDT"
        assert interval == "1m"
        assert 1 <= limit <= 200
        calls.append((limit, end_time_ms))
        eligible = [c for c in candles if end_time_ms is None or c.time_ms <= end_time_ms]
        return eligible[-limit:]

    monkeypatch.setattr(svc, "_fetch_klines_window", fake_window)
    monkeypatch.setattr(svc, "_now_ms", lambda: candles[-1].time_ms + 2 * step)
    result = await svc.fetch_kline_bundle("BTCUSDT", "1m", 600)
    assert result.data_complete
    assert len(result.closed) == 600
    assert result.closed[0].time_ms == candles[-600].time_ms
    assert result.closed[-1].time_ms == candles[-1].time_ms
    assert [size for size, _ in calls] == [200, 200, 200]


@pytest.mark.asyncio
async def test_single_page_retains_existing_behavior(monkeypatch):
    monkeypatch.setattr(MarketDataService, "fetch_kline_bundle", _REAL_FETCH_KLINE_BUNDLE)
    svc = MarketDataService()
    calls = []
    async def fake_window(symbol, interval, limit=200, end_time_ms=None):
        calls.append((limit, end_time_ms))
        return [Candle(time_ms=1_800_000_000_000, open=1, high=2, low=1, close=2, volume=1)]
    monkeypatch.setattr(svc, "_fetch_klines_window", fake_window)
    monkeypatch.setattr(svc, "_now_ms", lambda: 1_800_000_120_000)
    result = await svc.fetch_kline_bundle("BTCUSDT", "5m", 120)
    assert len(result.closed) == 0  # This 5m bar is still forming.
    assert calls == [(120, None)]
