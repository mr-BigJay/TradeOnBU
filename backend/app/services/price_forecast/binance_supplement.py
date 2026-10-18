from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

BINANCE_FAPI = "https://fapi.binance.com"


async def fetch_binance_open_interest_hist(symbol: str = "BTCUSDT", *, limit: int = 24) -> dict[str, Any]:
    """Public Binance futures OI history — always labeled binance, not Bitunix."""
    url = f"{BINANCE_FAPI}/futures/data/openInterestHist"
    params = {"symbol": symbol, "period": "1h", "limit": min(limit, 500)}
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        rows = resp.json()
    if not isinstance(rows, list):
        return {"source": "binance_futures", "status": "unavailable", "reason": "unexpected_response"}
    compact = []
    for row in rows[-12:]:
        try:
            compact.append(
                {
                    "time_ms": int(row["timestamp"]),
                    "open_interest": float(row["sumOpenInterest"]),
                    "oi_value_usd": float(row.get("sumOpenInterestValue") or 0),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return {
        "source": "binance_futures",
        "status": "ok" if compact else "unavailable",
        "symbol": symbol,
        "period": "1h",
        "points": compact,
        "note": "Binance USD-M open interest — not Bitunix OI",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


async def fetch_binance_liquidations_snapshot(symbol: str = "BTCUSDT") -> dict[str, Any]:
    """
    Binance public liquidation stream is WebSocket-only; REST forceOrders requires keys.
    We do not fabricate heatmaps — mark unavailable explicitly.
    """
    return {
        "source": "binance_futures",
        "status": "unavailable",
        "symbol": symbol,
        "reason": "public_rest_liquidation_feed_not_available_without_auth",
        "note": "Real liquidation events not exposed via unauthenticated REST in this integration",
    }
