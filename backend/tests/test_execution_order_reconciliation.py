from __future__ import annotations

from app.domain.candle_utils import candle_close_ms
from app.domain.candles import Candle
from app.domain.execution_order_policy import reconcile_pending_ai_limit_order

INTERVAL_5M = 300_000
INTERVAL_1M = 60_000


def test_invalidation_before_fill_by_close_time():
    order_ms = 1_000_000
    inv = Candle(time_ms=order_ms, open=95, high=96, low=91, close=92, volume=1)
    pre_inv_1m = [
        Candle(time_ms=order_ms + i * INTERVAL_1M, open=98, high=99, low=97.5, close=98.5, volume=1)
        for i in range(1, 6)
    ]
    fill = Candle(time_ms=order_ms + INTERVAL_5M + 60_000, open=99.5, high=100.5, low=99, close=100.2, volume=1)
    now_ms = candle_close_ms(fill, "1m") + 1_000
    out = reconcile_pending_ai_limit_order(
        order_ms=order_ms,
        direction="LONG",
        invalidation_level=93.0,
        monitor_cursor_ms=order_ms - INTERVAL_5M,
        limit_price=100.0,
        closed_1m=[*pre_inv_1m, fill],
        closed_5m=[inv],
        now_ms=now_ms,
        five_m_data_verified=True,
        one_m_data_complete=True,
    )
    assert out.action == "cancel"


def test_invalidation_cancel_suspended_when_1m_history_insufficient():
    order_ms = 1_000_000
    inv = Candle(time_ms=order_ms + INTERVAL_5M, open=95, high=96, low=91, close=92, volume=1)
    inv_close = candle_close_ms(inv, "5m")
    now_ms = inv_close + 5_000
    out = reconcile_pending_ai_limit_order(
        order_ms=order_ms,
        direction="LONG",
        invalidation_level=93.0,
        monitor_cursor_ms=order_ms - INTERVAL_5M,
        limit_price=100.0,
        closed_1m=[],
        closed_5m=[inv],
        now_ms=now_ms,
        five_m_data_verified=True,
        one_m_data_complete=False,
    )
    assert out.action == "suspend"
    assert out.reason == "insufficient_1m_history_for_invalidation_cancel"


def test_fill_before_invalidation_by_close_time():
    order_ms = 1_000_000
    fill = Candle(time_ms=order_ms + 60_000, open=99.5, high=100.5, low=99, close=100.2, volume=1)
    inv = Candle(time_ms=order_ms + INTERVAL_5M, open=95, high=96, low=91, close=92, volume=1)
    now_ms = max(candle_close_ms(fill, "1m"), candle_close_ms(inv, "5m")) + 1_000
    out = reconcile_pending_ai_limit_order(
        order_ms=order_ms,
        direction="LONG",
        invalidation_level=93.0,
        monitor_cursor_ms=order_ms - INTERVAL_5M,
        limit_price=100.0,
        closed_1m=[fill],
        closed_5m=[inv],
        now_ms=now_ms,
        five_m_data_verified=True,
    )
    assert out.action == "fill"
    assert out.fill_bar_1m is not None


def test_missing_5m_data_fails_closed():
    out = reconcile_pending_ai_limit_order(
        order_ms=1_000_000,
        direction="LONG",
        invalidation_level=93.0,
        monitor_cursor_ms=0,
        limit_price=100.0,
        closed_1m=[],
        closed_5m=[],
        now_ms=2_000_000,
        five_m_data_verified=False,
    )
    assert out.action == "suspend"
    assert out.reason == "missing_or_incomplete_5m_data"
