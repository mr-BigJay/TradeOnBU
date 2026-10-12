from __future__ import annotations

from app.domain.candles import Candle
from app.services.ict_structure import build_chart_overlays
from app.services.market_data import MarketDataService


class ChartDataService:
    def __init__(self, market: MarketDataService | None = None) -> None:
        self.market = market or MarketDataService()

    @staticmethod
    def serialize_candles(candles: list[Candle]) -> list[dict]:
        return [
            {"time_ms": c.time_ms, "open": c.open, "high": c.high, "low": c.low, "close": c.close}
            for c in candles
        ]

    async def bundle(self, symbol: str, interval: str, limit: int = 200, direction: str | None = None) -> dict:
        bundle = await self.market.fetch_kline_bundle(symbol, interval, limit)
        closed = bundle.closed[-limit:]
        overlays = build_chart_overlays(closed, direction)
        return {
            "symbol": symbol,
            "interval": interval,
            "candles": self.serialize_candles(closed),
            "overlays": overlays,
            "paper_only": True,
            "forming": bundle.forming is not None,
        }
