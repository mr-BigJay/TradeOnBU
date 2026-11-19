"""Aged pending-limit target checks require complete post-order 1m evidence.

All candles here are synthetic; this tests ordering and data coverage, not P&L.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.candles import Candle
from app.services.opportunity_monitor import _reconcile_pending_with_history

STEP = 60_000
ORDER_MS = 1_800_000_000_000


def bar(index, *, target=False, touch_limit=False):
    return Candle(
        time_ms=ORDER_MS + index * STEP,
        open=105,
        high=111 if target else 106,
        low=99 if touch_limit else (107 if target else 104),
        close=110 if target else 105,
        volume=1,
    )


def fixture_bundles(*, gap=False, same_minute=False, target=True):
    bars = [bar(i) for i in range(240)]
    if target:
        bars[230] = bar(230, target=True, touch_limit=same_minute)
    recent = SimpleNamespace(closed=bars[-120:], data_complete=True, gaps_remaining=[])
    if gap:
        bars.pop(45)
    history = SimpleNamespace(
        closed=bars,
        data_complete=not gap,
        gaps_remaining=[ORDER_MS + 45 * STEP] if gap else [],
    )
    return recent, history


def arguments():
    return dict(
        direction="LONG",
        invalidation_level=95.0,
        monitor_cursor_ms=ORDER_MS - 5 * STEP,
        limit_price=100.0,
        closed_5m=[],
        five_m_data_verified=True,
        target_price=110.0,
        entry_zone=(99.5, 100.5),
    )


@pytest.mark.asyncio
async def test_aged_pending_order_recovers_chronology_before_cancel():
    recent, history = fixture_bundles()
    market = SimpleNamespace(fetch_historical_closed=AsyncMock(return_value=history))
    result = await _reconcile_pending_with_history(
        market=market, symbol="BTCUSDT", order_ms=ORDER_MS,
        now_ms=ORDER_MS + 240 * STEP,
        recent_bundle=recent, reconcile_args=arguments(),
    )
    assert result.action == "cancel"
    assert result.reason == "target_reached_before_fill"
    market.fetch_historical_closed.assert_awaited_once()
    call = market.fetch_historical_closed.await_args
    assert call.args[:3] == ("BTCUSDT", "1m", ORDER_MS)
    assert call.kwargs["max_bars"] >= 240


@pytest.mark.asyncio
async def test_missing_backfill_candle_suspends_instead_of_inventing_fill_or_cancel():
    recent, history = fixture_bundles(gap=True)
    market = SimpleNamespace(fetch_historical_closed=AsyncMock(return_value=history))
    result = await _reconcile_pending_with_history(
        market=market, symbol="BTCUSDT", order_ms=ORDER_MS,
        now_ms=ORDER_MS + 240 * STEP,
        recent_bundle=recent, reconcile_args=arguments(),
    )
    assert result.action == "suspend"
    assert result.reason == "insufficient_1m_history_for_target_cancel"


@pytest.mark.asyncio
async def test_same_minute_target_and_entry_remains_ambiguous_without_backfill():
    recent, history = fixture_bundles(same_minute=True)
    market = SimpleNamespace(fetch_historical_closed=AsyncMock(return_value=history))
    result = await _reconcile_pending_with_history(
        market=market, symbol="BTCUSDT", order_ms=ORDER_MS,
        now_ms=ORDER_MS + 240 * STEP,
        recent_bundle=recent, reconcile_args=arguments(),
    )
    assert result.action == "suspend"
    assert result.reason == "target_fill_chronology_ambiguous"
    market.fetch_historical_closed.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_terminal_event_keeps_order_pending_without_expensive_backfill():
    recent, history = fixture_bundles(target=False)
    market = SimpleNamespace(fetch_historical_closed=AsyncMock(return_value=history))
    result = await _reconcile_pending_with_history(
        market=market, symbol="BTCUSDT", order_ms=ORDER_MS,
        now_ms=ORDER_MS + 240 * STEP,
        recent_bundle=recent, reconcile_args=arguments(),
    )
    assert result.action == "hold"
    market.fetch_historical_closed.assert_not_awaited()
