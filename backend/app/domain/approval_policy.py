"""Entry monitoring policy after Telegram approval.

If approval happens while the current 5m candle is still forming, we:
1) scan 1m closed candles from that 5m open through approval for entry touches;
2) begin 5m entry monitoring only from the **next** 5m candle open (not the in-flight bar).
"""

from __future__ import annotations

from app.domain.candle_utils import interval_ms, is_candle_closed


def next_interval_open_ms(timestamp_ms: int, interval: str = "5m") -> int:
    step = interval_ms(interval)
    return ((timestamp_ms // step) + 1) * step


def entry_monitor_start_ms(approval_ms: int, interval: str = "5m") -> int:
    """First interval open at or after approval — monitoring starts here."""
    step = interval_ms(interval)
    if approval_ms % step == 0:
        return approval_ms
    return next_interval_open_ms(approval_ms, interval)


def approval_inside_forming_bar(approval_ms: int, interval: str, now_ms: int) -> bool:
    step = interval_ms(interval)
    bar_open = (approval_ms // step) * step
    from app.domain.candles import Candle

    synthetic = Candle(open=0, high=0, low=0, close=0, time_ms=bar_open)
    return not is_candle_closed(synthetic, interval, now_ms)
