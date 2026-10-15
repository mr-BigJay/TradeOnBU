from __future__ import annotations

from datetime import datetime

from app.domain.candle_utils import interval_ms
from app.domain.time_utils import ensure_utc


def first_eligible_bar_open_ms(created_at: datetime, interval: str = "5m") -> int:
    """First bar open time (ms) strictly after setup creation — no pre-creation history."""
    created = ensure_utc(created_at)
    if created is None:
        return 0
    ms = int(created.timestamp() * 1000)
    step = interval_ms(interval)
    return ((ms // step) + 1) * step
