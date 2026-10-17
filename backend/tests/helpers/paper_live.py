from __future__ import annotations

from datetime import datetime, timezone


def fresh_ticker(price: float = 100.0) -> tuple[float, int]:
    ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return price, ms
