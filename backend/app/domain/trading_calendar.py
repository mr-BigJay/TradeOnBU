"""New BTCUSDT entry windows. Open positions remain protected at all hours."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TEHRAN = ZoneInfo("Asia/Tehran")


def new_entries_allowed(at: datetime) -> bool:
    """Monday-Friday in Tehran local time; Saturday/Sunday start no new entries.

    This is not an exchange closure: BTC trades 24/7. Only our policy blocks
    *new* orders and scheduled proposals; open SL/TP management remains active.
    """
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(TEHRAN).weekday() < 5
