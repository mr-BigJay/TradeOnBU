from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.config import Settings, get_settings
from app.domain.candle_utils import detect_gaps, interval_ms, split_closed_and_forming
from app.domain.candles import Candle

VALID_INTERVALS = {"1m", "5m", "15m", "1h", "4h"}


@dataclass
class LiveQuote:
    price: float
    fetched_at: datetime
    receipt_time_ms: int
    exchange_time_ms: int | None
    stale: bool
    source: str


@dataclass
class KlineBundle:
    closed: list[Candle]
    forming: Candle | None
    gaps_detected: list[int]
    gaps_remaining: list[int]
    data_complete: bool
    interval: str


class MarketDataService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._last_good_quote: LiveQuote | None = None
        self._lock = asyncio.Lock()

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    async def fetch_ticker(self, symbol: str) -> tuple[float, int | None]:
        """Returns (lastPrice, exchange_timestamp_ms or None if not provided by API)."""
        url = f"{self.settings.bitunix_futures_base}/api/v1/futures/market/tickers"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, params={"symbols": symbol})
            resp.raise_for_status()
            body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"Bitunix ticker error: {body.get('msg')}")
        rows = body.get("data") or []
        if not rows:
            raise RuntimeError("Empty ticker response")
        row = rows[0]
        exchange_ts = row.get("time") or row.get("ts")
        exchange_ms = int(exchange_ts) if exchange_ts is not None else None
        return float(row["lastPrice"]), exchange_ms

    async def fetch_klines(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]:
        return await self.fetch_closed_klines(symbol, interval, limit)

    async def fetch_closed_klines(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]:
        if interval not in VALID_INTERVALS:
            raise ValueError(f"Unsupported interval: {interval}")
        bundle = await self.fetch_kline_bundle(symbol, interval, limit)
        if not bundle.data_complete:
            raise RuntimeError(f"Incomplete {interval} history — gaps remain")
        return bundle.closed[-limit:]

    async def _fetch_klines_window(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        end_time_ms: int | None = None,
    ) -> list[Candle]:
        params: dict[str, str | int] = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        url = f"{self.settings.bitunix_futures_base}/api/v1/futures/market/kline"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"Bitunix kline error: {body.get('msg')}")
        candles = []
        for row in body.get("data") or []:
            # Bitunix futures returns baseVol/quoteVol (legacy payloads use volume/vol).
            vol_raw = next(
                (row[key] for key in ("baseVol", "volume", "vol") if row.get(key) is not None),
                None,
            )
            volume = float(vol_raw) if vol_raw is not None else None
            candles.append(
                Candle(
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    time_ms=int(row["time"]),
                    volume=volume,
                )
            )
        candles.sort(key=lambda c: c.time_ms)
        return candles

    async def fetch_historical_closed(
        self,
        symbol: str,
        interval: str,
        from_ms: int,
        max_bars: int = 500,
    ) -> KlineBundle:
        """Closed candles from from_ms through now, with gap backfill in that span."""
        now_ms = self._now_ms()
        step = interval_ms(interval)
        if from_ms > now_ms:
            from_ms = now_ms

        merged: dict[int, Candle] = {}
        end_time: int | None = None
        max_pages = max(12, (max_bars // 200) + 2)
        for _ in range(max_pages):
            batch = await self._fetch_klines_window(symbol, interval, limit=200, end_time_ms=end_time)
            if not batch:
                break
            for c in batch:
                if c.time_ms >= from_ms - step:
                    merged[c.time_ms] = c
            oldest = batch[0].time_ms
            if oldest <= from_ms or len(batch) < 200:
                break
            end_time = oldest - 1
            if len(merged) >= max_bars:
                break

        candles = sorted(merged.values(), key=lambda c: c.time_ms)
        gaps_detected = detect_gaps(candles, interval)
        for missing_start in gaps_detected[:12]:
            if missing_start < from_ms - step:
                continue
            older = await self._fetch_klines_window(
                symbol,
                interval,
                limit=200,
                end_time_ms=missing_start + step,
            )
            for c in older:
                merged[c.time_ms] = c
        candles = sorted(merged.values(), key=lambda c: c.time_ms)

        in_range = [c for c in candles if c.time_ms >= from_ms]
        gaps_remaining = detect_gaps(in_range, interval) if len(in_range) >= 2 else []
        closed, forming = split_closed_and_forming(candles, interval, now_ms)
        closed = [c for c in closed if c.time_ms >= from_ms]

        if in_range and in_range[0].time_ms > from_ms + step:
            gaps_remaining = [from_ms] + gaps_remaining

        covers_start = (not in_range) or (in_range[0].time_ms <= from_ms)
        data_complete = len(gaps_remaining) == 0 and covers_start
        if len(merged) >= max_bars and in_range and in_range[0].time_ms > from_ms:
            data_complete = False
            if from_ms not in gaps_remaining:
                gaps_remaining = [from_ms] + list(gaps_remaining)

        return KlineBundle(
            closed=closed,
            forming=forming,
            gaps_detected=gaps_detected,
            gaps_remaining=gaps_remaining,
            data_complete=data_complete,
            interval=interval,
        )

    async def fetch_kline_bundle(self, symbol: str, interval: str, limit: int = 200) -> KlineBundle:
        if interval not in VALID_INTERVALS or limit < 1:
            raise ValueError(f"Unsupported candle request: {interval} limit={limit}")
        now_ms = self._now_ms()
        # The futures kline endpoint accepts at most 200 candles per request.
        # BALANCED confirmation and paper fill recovery often need >200.
        # Page backwards rather than sending an invalid oversized limit.
        merged_pages: dict[int, Candle] = {}
        remaining = limit
        end_time_ms: int | None = None
        while remaining > 0:
            page_size = min(remaining, 200)
            page = await self._fetch_klines_window(
                symbol, interval, limit=page_size, end_time_ms=end_time_ms
            )
            if not page:
                break
            previous_size = len(merged_pages)
            merged_pages.update({bar.time_ms: bar for bar in page})
            oldest = page[0].time_ms
            if len(merged_pages) == previous_size:
                # Protect against an endpoint that ignores endTime.
                break
            if len(page) < page_size:
                break
            end_time_ms = oldest - 1
            remaining = max(0, limit - len(merged_pages))
        candles = sorted(merged_pages.values(), key=lambda c: c.time_ms)[-limit:]
        gaps_detected = detect_gaps(candles, interval)
        merged: dict[int, Candle] = {c.time_ms: c for c in candles}
        for missing_start in gaps_detected[:8]:
            older = await self._fetch_klines_window(
                symbol,
                interval,
                limit=min(limit, 200),
                end_time_ms=missing_start + interval_ms(interval),
            )
            for c in older:
                merged[c.time_ms] = c
        candles = sorted(merged.values(), key=lambda c: c.time_ms)[-limit:]
        gaps_remaining = detect_gaps(candles, interval)
        closed, forming = split_closed_and_forming(candles, interval, now_ms)
        return KlineBundle(
            closed=closed,
            forming=forming,
            gaps_detected=gaps_detected,
            gaps_remaining=gaps_remaining,
            data_complete=len(gaps_remaining) == 0,
            interval=interval,
        )

    async def fetch_live_price(self, symbol: str) -> LiveQuote:
        async with self._lock:
            fetched_at = datetime.now(timezone.utc)
            receipt_ms = self._now_ms()
            try:
                price, exchange_ms = await self.fetch_ticker(symbol)
                quote = LiveQuote(
                    price=price,
                    fetched_at=fetched_at,
                    receipt_time_ms=receipt_ms,
                    exchange_time_ms=exchange_ms,
                    stale=False,
                    source="bitunix_ticker",
                )
                self._last_good_quote = quote
                return quote
            except Exception:
                if self._last_good_quote:
                    return LiveQuote(
                        price=self._last_good_quote.price,
                        fetched_at=self._last_good_quote.fetched_at,
                        receipt_time_ms=self._last_good_quote.receipt_time_ms,
                        exchange_time_ms=self._last_good_quote.exchange_time_ms,
                        stale=True,
                        source=self._last_good_quote.source,
                    )
                raise

    def is_quote_stale(self, quote: LiveQuote | None) -> bool:
        if quote is None:
            return True
        if quote.stale:
            return True
        receipt_age = (self._now_ms() - quote.receipt_time_ms) / 1000
        if receipt_age > self.settings.market_data_stale_seconds:
            return True
        if quote.exchange_time_ms is not None:
            exch_age = (self._now_ms() - quote.exchange_time_ms) / 1000
            if exch_age > self.settings.market_data_stale_seconds:
                return True
        return False
