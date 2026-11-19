"""Closed 1m chronology must cover signed analyst confirmations before order.

Paper-only, synthetic candles; no market data or real order is requested.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.candles import Candle
from app.services import market_data
from app.services.paper_auto_trading import _verified_freshness_minutes

STEP = 60_000
START = 1_800_000_000_000


def minute(i):
    return Candle(
        open=1000.0, high=1001.0, low=999.0, close=1000.0,
        time_ms=START + i * STEP, volume=1.0,
    )


@pytest.mark.asyncio
async def test_recent_confirmations_reuse_small_window(monkeypatch):
    recent = SimpleNamespace(
        closed=[minute(i) for i in range(120, 240)],
        data_complete=True, gaps_remaining=[],
    )
    fetch_recent = AsyncMock(return_value=recent)
    fetch_history = AsyncMock()
    monkeypatch.setattr(market_data.MarketDataService, "fetch_kline_bundle", fetch_recent)
    monkeypatch.setattr(market_data.MarketDataService, "fetch_historical_closed", fetch_history)
    result = await _verified_freshness_minutes(
        "BTCUSDT", since_ms=START + 220 * STEP, as_of_ms=START + 240 * STEP
    )
    assert result is recent
    fetch_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_aged_confirmation_backfills_from_actual_trigger(monkeypatch):
    recent = SimpleNamespace(
        closed=[minute(i) for i in range(120, 240)],
        data_complete=True, gaps_remaining=[],
    )
    history = SimpleNamespace(
        closed=[minute(i) for i in range(20, 240)],
        data_complete=True, gaps_remaining=[],
    )
    fetch_recent = AsyncMock(return_value=recent)
    fetch_history = AsyncMock(return_value=history)
    monkeypatch.setattr(market_data.MarketDataService, "fetch_kline_bundle", fetch_recent)
    monkeypatch.setattr(market_data.MarketDataService, "fetch_historical_closed", fetch_history)
    result = await _verified_freshness_minutes(
        "BTCUSDT", since_ms=START + 20 * STEP, as_of_ms=START + 240 * STEP
    )
    assert result is history
    fetch_history.assert_awaited_once()
    args = fetch_history.await_args
    assert args.args == ("BTCUSDT", "1m", START + 20 * STEP)
    assert args.kwargs["max_bars"] >= 220


@pytest.mark.asyncio
async def test_missing_exchange_history_stays_fail_closed(monkeypatch):
    recent = SimpleNamespace(
        closed=[minute(i) for i in range(120, 240)],
        data_complete=True, gaps_remaining=[],
    )
    broken = SimpleNamespace(
        closed=[minute(i) for i in range(40, 240)],
        data_complete=False, gaps_remaining=[START + 35 * STEP],
    )
    monkeypatch.setattr(
        market_data.MarketDataService,
        "fetch_kline_bundle", AsyncMock(return_value=recent),
    )
    monkeypatch.setattr(
        market_data.MarketDataService,
        "fetch_historical_closed", AsyncMock(return_value=broken),
    )
    result = await _verified_freshness_minutes(
        "BTCUSDT", since_ms=START + 20 * STEP, as_of_ms=START + 240 * STEP
    )
    assert result.data_complete is False


@pytest.mark.asyncio
async def test_no_unbounded_1m_history_fetch(monkeypatch):
    recent = SimpleNamespace(
        closed=[minute(i) for i in range(120, 240)],
        data_complete=True, gaps_remaining=[],
    )
    fetch_history = AsyncMock()
    monkeypatch.setattr(
        market_data.MarketDataService,
        "fetch_kline_bundle", AsyncMock(return_value=recent),
    )
    monkeypatch.setattr(
        market_data.MarketDataService, "fetch_historical_closed", fetch_history,
    )
    result = await _verified_freshness_minutes(
        "BTCUSDT", since_ms=START, as_of_ms=START + 2000 * STEP
    )
    assert result is recent
    fetch_history.assert_not_awaited()
