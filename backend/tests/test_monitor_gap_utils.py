from app.domain.candles import Candle
from app.services.position.monitor_gap_utils import (
    INTERVAL_1M_MS,
    can_resume_from_suspension,
    leading_history_gap,
    missing_interval_before_bar,
    next_expected_bar_open_ms,
)


def test_next_expected_after_processed_bar():
    t0 = 1_700_000_000_000
    assert next_expected_bar_open_ms(t0) == t0 + INTERVAL_1M_MS


def test_contiguous_next_candle_not_a_leading_gap():
    t0 = 1_700_000_000_000
    t1 = t0 + INTERVAL_1M_MS
    assert leading_history_gap(t0, t1) is False


def test_missing_minute_is_leading_gap():
    t0 = 1_700_000_000_000
    t2 = t0 + 2 * INTERVAL_1M_MS
    assert leading_history_gap(t0, t2) is True


def test_internal_missing_interval():
    t0 = 1_700_000_000_000
    t1 = t0 + INTERVAL_1M_MS
    t3 = t0 + 3 * INTERVAL_1M_MS
    assert missing_interval_before_bar(t0, t1) is None
    assert missing_interval_before_bar(t1, t3) == t0 + 2 * INTERVAL_1M_MS


def test_can_resume_when_chain_complete():
    t0 = 1_700_000_000_000
    closed = [
        Candle(1, 1, 1, 1, t0 + INTERVAL_1M_MS, 1),
        Candle(1, 1, 1, 1, t0 + 2 * INTERVAL_1M_MS, 1),
    ]
    assert can_resume_from_suspension(t0, closed, 0, []) is True
