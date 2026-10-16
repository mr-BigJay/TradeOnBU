"""Block live exchange HTTP in backend tests; safe empty defaults on MarketDataService."""

from __future__ import annotations

from typing import Any

from app.domain.candles import Candle
from app.services.market_data import KlineBundle, LiveQuote


async def _offline_default_fetch_ticker(self, symbol: str) -> tuple[float, int | None]:
    override = getattr(self, "_test_ticker_price", None)
    if override is not None:
        return float(override), getattr(self, "_test_ticker_ts", None)
    return 100.0, None


def _empty_bundle(interval: str = "5m") -> KlineBundle:
    return KlineBundle(
        closed=[],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval=interval,
    )


async def _offline_fetch_kline_bundle(self, symbol: str, interval: str, limit: int = 200) -> KlineBundle:
    override = getattr(self, "_test_kline_bundle", None)
    if override is not None:
        return override
    return _empty_bundle(interval)


async def _offline_fetch_klines(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]:
    return []


async def _offline_fetch_historical_closed(
    self,
    symbol: str,
    interval: str,
    *,
    from_ms: int,
    max_bars: int,
) -> KlineBundle:
    return _empty_bundle(interval)


async def _offline_fetch_live_price(self, symbol: str) -> LiveQuote:
    from datetime import datetime, timezone

    price = getattr(self, "_test_ticker_price", None) or 100.0
    now = datetime.now(timezone.utc)
    return LiveQuote(
        price=float(price),
        fetched_at=now,
        receipt_time_ms=int(now.timestamp() * 1000),
        exchange_time_ms=None,
        stale=False,
        source="test_offline_stub",
    )


def install_offline_market_guards(monkeypatch) -> None:
    from app.services.market_data import MarketDataService

    monkeypatch.setattr(MarketDataService, "fetch_ticker", _offline_default_fetch_ticker)
    monkeypatch.setattr(MarketDataService, "fetch_kline_bundle", _offline_fetch_kline_bundle)
    monkeypatch.setattr(MarketDataService, "fetch_klines", _offline_fetch_klines)
    monkeypatch.setattr(MarketDataService, "fetch_historical_closed", _offline_fetch_historical_closed)
    monkeypatch.setattr(MarketDataService, "fetch_live_price", _offline_fetch_live_price)
