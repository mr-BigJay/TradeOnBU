import pytest

from app.domain.candles import Candle
from app.services.entry_detection import EntryDetectionService
from app.services.ict_structure import validate_ict_sequence


def _c(open_, high, low, close, t) -> Candle:
    return Candle(open=open_, high=high, low=low, close=close, time_ms=t)


def test_reject_without_sweep_sequence():
    candles = [_c(100, 101, 99, 100.5, i * 300_000) for i in range(40)]
    seq = validate_ict_sequence(candles, "LONG")
    assert seq.ok is False
    assert seq.reason in {"NO_LIQUIDITY_LEVEL", "NO_VALID_SWEEP", "NO_MSS", "NO_DISPLACEMENT", "NO_FVG_AFTER_SEQUENCE"}


@pytest.mark.asyncio
async def test_entry_scan_rejects_incomplete_setup():
    from app.services.entry_detection import EntryDetectionService
    from app.services.paper_execution import PaperExecutionEngine

    class FakeTrendEngine:
        async def analyze(self, symbol):
            from app.models import TrendState
            from app.services.trend_engine import TrendContext
            from datetime import datetime, timezone

            return TrendContext(
                symbol=symbol,
                trend_4h=TrendState.BULLISH,
                trend_15m=TrendState.BULLISH,
                trend_5m=TrendState.BULLISH,
                ict_bias=TrendState.BULLISH,
                details={},
                recorded_at=datetime.now(timezone.utc),
            )

    class FakeMarket:
        async def fetch_klines(self, symbol, interval, limit=200):
            return [_c(100, 101, 99, 100.2, i * 300_000) for i in range(60)]

    svc = EntryDetectionService(FakeMarket(), FakeTrendEngine(), PaperExecutionEngine())
    out = await svc.scan("BTCUSDT", 2.0)
    assert out is None or out.rejected_reason is not None
