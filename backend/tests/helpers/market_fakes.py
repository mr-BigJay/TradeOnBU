from datetime import datetime, timezone

from app.domain.candles import Candle
from app.services.market_data import KlineBundle, LiveQuote


def complete_bundle(
    closed: list[Candle] | None = None,
    *,
    interval: str = "5m",
    forming: Candle | None = None,
) -> KlineBundle:
    return KlineBundle(
        closed=closed or [],
        forming=forming,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval=interval,
    )


def live_quote(price: float = 100.0) -> LiveQuote:
    receipt = int(datetime.now(timezone.utc).timestamp() * 1000)
    return LiveQuote(
        price=price,
        fetched_at=datetime.now(timezone.utc),
        receipt_time_ms=receipt,
        exchange_time_ms=None,
        stale=False,
        source="test",
    )


class EmptyCompleteMarket:
    """Market stub with complete empty history (approval precheck passes if entry not touched)."""

    async def fetch_kline_bundle(self, symbol: str, interval: str, limit: int = 200) -> KlineBundle:
        return complete_bundle(interval=interval)

    async def fetch_historical_closed(self, symbol: str, interval: str, from_ms: int, max_bars: int = 500) -> KlineBundle:
        return complete_bundle(interval=interval)

    async def fetch_live_price(self, symbol: str) -> LiveQuote:
        return live_quote()

    def is_quote_stale(self, quote: LiveQuote | None) -> bool:
        return False


class PrecheckTouchMarket:
    """Returns 1m/5m bundles; touch is expressed on 1m for creation-boundary accuracy."""

    def __init__(self, touch_bar: Candle, *, use_5m: bool = False) -> None:
        self.touch_bar = touch_bar
        self.use_5m = use_5m

    async def fetch_kline_bundle(self, symbol: str, interval: str, limit: int = 200) -> KlineBundle:
        if interval == "1m":
            return complete_bundle(closed=[self.touch_bar], interval="1m")
        if self.use_5m:
            return complete_bundle(closed=[self.touch_bar], interval="5m")
        return complete_bundle(interval="5m")

    async def fetch_live_price(self, symbol: str) -> LiveQuote:
        return live_quote()

    def is_quote_stale(self, quote: LiveQuote | None) -> bool:
        return False
