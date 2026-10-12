from __future__ import annotations

from app.domain.candles import Candle
from app.services.ict_structure import build_chart_overlays, confirmed_candles
from app.services.market_data import MarketDataService


class ChartDataService:
    def __init__(self, market: MarketDataService | None = None) -> None:
        self.market = market or MarketDataService()

    @staticmethod
    def serialize_candles(candles: list[Candle]) -> list[dict]:
        confirmed = confirmed_candles(candles)
        return [
            {
                "time_ms": c.time_ms,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
            }
            for c in confirmed
        ]

    async def bundle(self, symbol: str, interval: str, limit: int = 200, direction: str | None = None) -> dict:
        candles = await self.market.fetch_klines(symbol, interval, limit)
        overlays = build_chart_overlays(candles, direction)
        return {
            "symbol": symbol,
            "interval": interval,
            "candles": self.serialize_candles(candles),
            "overlays": overlays,
            "paper_only": True,
        }
