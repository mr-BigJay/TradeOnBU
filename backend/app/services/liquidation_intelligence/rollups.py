from __future__ import annotations

import math
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.liquidation_intelligence.analytics import WINDOW_MS, _Acc, _events_in_window
from app.services.liquidation_intelligence.repository import fetch_events_since, upsert_window_rollup


def _bucket_start_ms(now_ms: int, window_ms: int) -> int:
    return (now_ms // window_ms) * window_ms


async def materialize_rollups_for_symbol(
    session: AsyncSession,
    *,
    symbol: str = "BTCUSDT",
    now_ms: int,
    sources: tuple[str, ...] = ("bybit", "binance", "combined"),
) -> int:
    """Idempotent rollup rows for the current bucket of each window."""
    lookback = max(WINDOW_MS.values())
    all_events = await fetch_events_since(session, symbol=symbol, since_ms=now_ms - lookback)
    written = 0
    for window_name, window_ms in WINDOW_MS.items():
        bucket = _bucket_start_ms(now_ms, window_ms)
        for source in sources:
            if source == "combined":
                evs = _events_in_window(all_events, now_ms, window_ms)
            else:
                evs = _events_in_window(
                    [e for e in all_events if str(e.source_exchange) == source],
                    now_ms,
                    window_ms,
                )
            acc = _Acc()
            for ev in evs:
                acc.add(str(ev.liquidated_position_side), float(ev.estimated_notional_usd))
            payload: dict[str, Any] = acc.to_dict()
            payload["coverage_event_count"] = len(evs)
            payload["bucket_end_ms"] = bucket + window_ms
            if not math.isfinite(payload.get("total_observed_notional_usd") or 0):
                continue
            await upsert_window_rollup(
                session,
                symbol=symbol,
                source_exchange=source,
                window_name=window_name,
                bucket_start_ms=bucket,
                payload=payload,
            )
            written += 1
    return written
