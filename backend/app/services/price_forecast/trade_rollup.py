from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MarketMicroRollup


def parse_trade_event_ms(tr: dict[str, Any]) -> int | None:
    for key in ("t", "ts"):
        raw = tr.get(key)
        if raw is None:
            continue
        if isinstance(raw, (int, float)):
            ms = int(raw)
            return ms if ms > 10_000_000_000 else ms * 1000
        if isinstance(raw, str):
            s = raw.strip()
            if s.isdigit():
                ms = int(s)
                return ms if ms > 10_000_000_000 else ms * 1000
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
    return None


async def upsert_trade_bucket(
    session: AsyncSession,
    *,
    symbol: str,
    bucket_start_ms: int,
    buy_volume: float,
    sell_volume: float,
    trade_count: int,
) -> None:
    row = (
        await session.execute(
            select(MarketMicroRollup).where(
                MarketMicroRollup.symbol == symbol,
                MarketMicroRollup.bucket_start_ms == bucket_start_ms,
                MarketMicroRollup.kind == "trade_5m",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(
            MarketMicroRollup(
                symbol=symbol,
                bucket_start_ms=bucket_start_ms,
                kind="trade_5m",
                source="bitunix_ws_trades",
                payload={
                    "buy_volume": buy_volume,
                    "sell_volume": sell_volume,
                    "trade_count": trade_count,
                },
            )
        )
        return
    prev = row.payload or {}
    row.payload = {
        "buy_volume": float(prev.get("buy_volume") or 0) + buy_volume,
        "sell_volume": float(prev.get("sell_volume") or 0) + sell_volume,
        "trade_count": int(prev.get("trade_count") or 0) + trade_count,
    }
