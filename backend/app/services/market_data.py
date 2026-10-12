from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.config import Settings, get_settings
from app.domain.candles import Candle

VALID_INTERVALS = {"1m", "5m", "15m", "1h", "4h"}


@dataclass
class LiveQuote:
    price: float
    fetched_at: datetime
    stale: bool
    source: str


class MarketDataService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._last_quote: LiveQuote | None = None
        self._lock = asyncio.Lock()

    async def fetch_klines(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]:
        if interval not in VALID_INTERVALS:
            raise ValueError(f"Unsupported interval: {interval}")
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        url = f"{self.settings.bitunix_futures_base}/api/v1/futures/market/kline"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"Bitunix kline error: {body.get('msg')}")
        rows = body.get("data") or []
        candles: list[Candle] = []
        for row in rows:
            candles.append(
                Candle(
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    time_ms=int(row["time"]),
                )
            )
        candles.sort(key=lambda c: c.time_ms)
        return candles

    async def fetch_live_price(self, symbol: str) -> LiveQuote:
        async with self._lock:
            try:
                candles = await self.fetch_klines(symbol, "5m", limit=2)
                if not candles:
                    raise RuntimeError("No candle data")
                confirmed = candles[:-1] if len(candles) > 1 else candles
                price = confirmed[-1].close
                fetched_at = datetime.now(timezone.utc)
                quote = LiveQuote(price=price, fetched_at=fetched_at, stale=False, source="bitunix_futures")
                self._last_quote = quote
                return quote
            except Exception:
                if self._last_quote:
                    age = (datetime.now(timezone.utc) - self._last_quote.fetched_at).total_seconds()
                    stale = age > self.settings.market_data_stale_seconds
                    return LiveQuote(
                        price=self._last_quote.price,
                        fetched_at=self._last_quote.fetched_at,
                        stale=True,
                        source=self._last_quote.source,
                    )
                raise

    def is_quote_stale(self, quote: LiveQuote | None) -> bool:
        if quote is None:
            return True
        if quote.stale:
            return True
        age = (datetime.now(timezone.utc) - quote.fetched_at).total_seconds()
        return age > self.settings.market_data_stale_seconds
