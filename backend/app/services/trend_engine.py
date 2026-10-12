from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MarketTrendSnapshot, TrendState
from app.services.ict_structure import analyze_structure
from app.services.market_data import MarketDataService


@dataclass
class TrendContext:
    symbol: str
    trend_4h: TrendState
    trend_15m: TrendState
    trend_5m: TrendState
    ict_bias: TrendState
    details: dict
    recorded_at: datetime


def combine_bias(t4: TrendState, t15: TrendState, t5: TrendState) -> TrendState:
    if t4 == TrendState.UNCERTAIN or t15 == TrendState.UNCERTAIN:
        return TrendState.UNCERTAIN
    if t4 == t15 == t5:
        return t4
    if t4 == t15:
        return t4
    if t4 == TrendState.SIDEWAYS or t15 == TrendState.SIDEWAYS:
        return TrendState.SIDEWAYS
    return TrendState.UNCERTAIN


class TrendEngine:
    def __init__(self, market: MarketDataService) -> None:
        self.market = market

    async def analyze(self, symbol: str) -> TrendContext:
        c4h = await self.market.fetch_klines(symbol, "4h", 120)
        c15 = await self.market.fetch_klines(symbol, "15m", 200)
        c5 = await self.market.fetch_klines(symbol, "5m", 200)

        s4 = analyze_structure(c4h)
        s15 = analyze_structure(c15)
        s5 = analyze_structure(c5)

        bias = combine_bias(s4.trend, s15.trend, s5.trend)
        details = {
            "4h": asdict(s4),
            "15m": asdict(s15),
            "5m": asdict(s5),
            "alignment": {
                "4h": s4.trend.value,
                "15m": s15.trend.value,
                "5m": s5.trend.value,
            },
        }
        for tf in ("4h", "15m", "5m"):
            if hasattr(details[tf]["trend"], "value"):
                details[tf]["trend"] = details[tf]["trend"].value

        return TrendContext(
            symbol=symbol,
            trend_4h=s4.trend,
            trend_15m=s15.trend,
            trend_5m=s5.trend,
            ict_bias=bias,
            details=details,
            recorded_at=datetime.now(timezone.utc),
        )

    async def persist(self, session: AsyncSession, ctx: TrendContext) -> MarketTrendSnapshot:
        row = MarketTrendSnapshot(
            symbol=ctx.symbol,
            trend_4h=ctx.trend_4h.value,
            trend_15m=ctx.trend_15m.value,
            trend_5m=ctx.trend_5m.value,
            ict_bias=ctx.ict_bias.value,
            details=ctx.details,
            recorded_at=ctx.recorded_at,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row
