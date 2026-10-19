from __future__ import annotations

from datetime import datetime

from app.domain.candle_utils import interval_ms
from app.domain.entry_execution import ENTRY_MODE_BALANCED, normalize_entry_mode
from app.domain.time_utils import ensure_utc


def first_eligible_bar_open_ms(created_at: datetime, interval: str = "5m") -> int:
    """First bar open time (ms) strictly after setup creation — forward monitor boundary."""
    created = ensure_utc(created_at)
    if created is None:
        return 0
    ms = int(created.timestamp() * 1000)
    step = interval_ms(interval)
    return ((ms // step) + 1) * step


def monitor_start_open_ms(created_at: datetime, entry_mode: str = "STRICT") -> int:
    """Monitor boundary at persist: 1m for BALANCED, 5m for STRICT (unchanged)."""
    mode = normalize_entry_mode(entry_mode)
    interval = "1m" if mode == ENTRY_MODE_BALANCED else "5m"
    return first_eligible_bar_open_ms(created_at, interval)


def bars_closed_at_or_before_ms(closed: list, persist_ms: int, interval: str = "5m") -> list:
    from app.domain.candle_utils import candle_close_ms

    out = [b for b in closed if candle_close_ms(b, interval) <= persist_ms]
    return sorted(out, key=lambda c: c.time_ms)


def immediate_replay_window_start_ms(persist_ms: int, interval: str = "5m", max_bars: int = 48) -> int:
    step = interval_ms(interval)
    return max(0, persist_ms - max_bars * step)
