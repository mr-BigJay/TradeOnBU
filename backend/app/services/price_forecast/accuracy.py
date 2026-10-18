from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.domain.candle_utils import interval_ms
from app.models import PriceForecastPoint
from app.services.market_data import MarketDataService


async def _build_close_map(symbol: str, open_ms_list: list[int]) -> dict[int, float]:
    if not open_ms_list:
        return {}
    market = MarketDataService()
    hour_ms = interval_ms("1h")
    min_open = min(open_ms_list)
    max_open = max(open_ms_list)
    span = max(10, (max_open - min_open) // hour_ms + 5)
    limit = min(200, span + 5)
    closed = await market.fetch_closed_klines(symbol, "1h", limit=limit)
    by_open = {int(c.time_ms): float(c.close) for c in closed}
    if all(open_ms in by_open for open_ms in open_ms_list):
        return by_open

    merged: dict[int, float] = dict(by_open)
    end_cursor = int(datetime.now(timezone.utc).timestamp() * 1000)
    for _ in range(8):
        if all(open_ms in merged for open_ms in open_ms_list):
            break
        older = await market._fetch_klines_window(symbol, "1h", limit=200, end_time_ms=end_cursor)
        for c in older:
            merged[int(c.time_ms)] = float(c.close)
        if not older:
            break
        end_cursor = min(int(c.time_ms) for c in older) - hour_ms
    return merged


async def record_actual_closes_for_due_points(*, symbol: str = "BTCUSDT", batch_size: int = 100) -> int:
    """Attach Bitunix 1H closes for due forecast points (handles long downtime)."""
    now = datetime.now(timezone.utc)
    updated = 0
    async with SessionLocal() as session:
        due = (
            await session.execute(
                select(PriceForecastPoint)
                .where(PriceForecastPoint.actual_close.is_(None))
                .where(PriceForecastPoint.target_time <= now)
                .order_by(PriceForecastPoint.target_time)
                .limit(batch_size)
            )
        ).scalars().all()
        if not due:
            return 0
        hour_ms = interval_ms("1h")
        open_ms_list = [int(pt.target_time.timestamp() * 1000) - hour_ms for pt in due]
        try:
            by_ms = await _build_close_map(symbol, open_ms_list)
        except Exception:
            return 0
        for pt, open_ms in zip(due, open_ms_list):
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
