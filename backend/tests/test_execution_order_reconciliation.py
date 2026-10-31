from __future__ import annotations

from app.domain.candle_utils import candle_close_ms
from app.domain.candles import Candle
from app.domain.execution_order_policy import reconcile_pending_ai_limit_order

INTERVAL_5M = 300_000
INTERVAL_1M = 60_000
ORDER_MS = 1_080_000  # 1m-aligned ms


def test_invalidation_before_fill_by_close_time():
    order_ms = ORDER_MS
    inv = Candle(time_ms=order_ms, open=95, high=96, low=91, close=92, volume=1)
    pre_inv_1m = [
        Candle(time_ms=order_ms + i * INTERVAL_1M, open=101.5, high=102, low=100.5, close=101.5, volume=1)
        for i in range(0, 6)
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
    order_ms = ORDER_MS
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


def test_invalidation_cancel_suspended_when_interior_1m_gap():
    """First/last 1m candles present but missing minutes in between must not cancel."""
    order_ms = ORDER_MS
    inv = Candle(time_ms=order_ms, open=95, high=96, low=91, close=92, volume=1)
    inv_close = candle_close_ms(inv, "5m")
    dense_low_high = dict(open=98, high=99, low=97.5, close=98.5, volume=1)
    gapped_1m = [
        Candle(time_ms=order_ms, **dense_low_high),
        Candle(time_ms=order_ms + INTERVAL_1M, **dense_low_high),
        Candle(time_ms=order_ms + 4 * INTERVAL_1M, **dense_low_high),
        Candle(time_ms=order_ms + 5 * INTERVAL_1M, **dense_low_high),
    ]
    now_ms = inv_close + 5_000
    out = reconcile_pending_ai_limit_order(
        order_ms=order_ms,
        direction="LONG",
        invalidation_level=93.0,
        monitor_cursor_ms=order_ms - INTERVAL_5M,
        limit_price=100.0,
        closed_1m=gapped_1m,
        closed_5m=[inv],
        now_ms=now_ms,
        five_m_data_verified=True,
        one_m_data_complete=True,
    )
    assert out.action == "suspend"
    assert out.reason == "insufficient_1m_history_for_invalidation_cancel"


def test_invalidation_cancel_suspended_when_gaps_remaining_in_window():
    order_ms = ORDER_MS
    inv = Candle(time_ms=order_ms, open=95, high=96, low=91, close=92, volume=1)
    inv_close = candle_close_ms(inv, "5m")
    dense = dict(open=98, high=99, low=97.5, close=98.5, volume=1)
    closed_1m = [
        Candle(time_ms=order_ms + i * INTERVAL_1M, **dense)
        for i in range(0, 6)
    ]
    gap_at = order_ms + 2 * INTERVAL_1M
    now_ms = inv_close + 5_000
    out = reconcile_pending_ai_limit_order(
        order_ms=order_ms,
        direction="LONG",
        invalidation_level=93.0,
        monitor_cursor_ms=order_ms - INTERVAL_5M,
        limit_price=100.0,
        closed_1m=closed_1m,
        closed_5m=[inv],
        now_ms=now_ms,
        five_m_data_verified=True,
        one_m_data_complete=True,
        gaps_remaining=[gap_at],
    )
    assert out.action == "suspend"
    assert out.reason == "insufficient_1m_history_for_invalidation_cancel"


def test_fill_before_invalidation_by_close_time():
    order_ms = ORDER_MS
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


def test_long_buy_limit_fills_on_candle_entirely_below_limit():
    from app.domain.execution_order_policy import bar_hits_limit

    bar = Candle(time_ms=ORDER_MS, open=98, high=99, low=97, close=98, volume=1)
    assert bar_hits_limit(bar, 100.0, "LONG")
    assert not bar_hits_limit(bar, 100.0, "SHORT")


def test_short_sell_limit_fills_on_candle_entirely_above_limit():
    from app.domain.execution_order_policy import bar_hits_limit

    bar = Candle(time_ms=ORDER_MS, open=102, high=103, low=101, close=102, volume=1)
    assert bar_hits_limit(bar, 100.0, "SHORT")
    assert not bar_hits_limit(bar, 100.0, "LONG")


def test_reconciliation_fills_marketable_buy_after_order_creation():
    order_ms = ORDER_MS
    # Earliest fully post-order candle is already below the resting buy limit.
    bar = Candle(time_ms=order_ms + INTERVAL_1M, open=98, high=99, low=97, close=98, volume=1)
    out = reconcile_pending_ai_limit_order(
        order_ms=order_ms, direction="LONG", invalidation_level=90,
        monitor_cursor_ms=order_ms - INTERVAL_5M, limit_price=100,
        closed_1m=[bar], closed_5m=[],
        now_ms=candle_close_ms(bar, "1m") + 1,
        five_m_data_verified=True,
    )
    assert out.action == "fill"
    assert out.fill_bar_1m == bar


def test_reconciliation_never_infers_fill_from_pre_order_minute():
    # A 1m candle that started before the order was created does not prove
    # the favorable excursion happened after the order.
    order_ms = ORDER_MS + 30_000
    bar = Candle(time_ms=ORDER_MS, open=98, high=99, low=97, close=98, volume=1)
    out = reconcile_pending_ai_limit_order(
        order_ms=order_ms, direction="LONG", invalidation_level=90,
        monitor_cursor_ms=ORDER_MS - INTERVAL_5M, limit_price=100,
        closed_1m=[bar], closed_5m=[],
        now_ms=candle_close_ms(bar, "1m") + 1,
        five_m_data_verified=True,
    )
    assert out.action == "hold"
