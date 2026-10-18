from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import PriceForecastPoint
from app.domain.candle_utils import interval_ms
from app.services.market_data import MarketDataService


async def record_actual_closes_for_due_points(*, symbol: str = "BTCUSDT") -> int:
    """After an hourly close, attach Bitunix 1H close to forecast points."""
    now = datetime.now(timezone.utc)
    market = MarketDataService()
    updated = 0
    async with SessionLocal() as session:
        due = (
            await session.execute(
                select(PriceForecastPoint)
                .where(PriceForecastPoint.actual_close.is_(None))
                .where(PriceForecastPoint.target_time <= now)
                .order_by(PriceForecastPoint.target_time)
                .limit(50)
            )
        ).scalars().all()
        if not due:
            return 0
        try:
            closed = await market.fetch_closed_klines(symbol, "1h", limit=10)
        except Exception:
            return 0
        by_ms = {int(c.time_ms): float(c.close) for c in closed}
        hour_ms = interval_ms("1h")
        for pt in due:
            target_ms = int(pt.target_time.timestamp() * 1000)
            open_ms = target_ms - hour_ms
            close = by_ms.get(open_ms)
            if close is None:
                continue
            pt.actual_close = close
            pt.actual_recorded_at = now
            pt.error_abs = abs(close - pt.forecast_price)
            pt.error_pct = (pt.error_abs / close) * 100 if close else None
            updated += 1
        await session.commit()
    return updated
