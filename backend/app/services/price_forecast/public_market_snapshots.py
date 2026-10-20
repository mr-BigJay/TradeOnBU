"""Public market snapshot collection independent of the retired GPT forecast."""

from __future__ import annotations

import logging

from app.db import SessionLocal
from app.services.price_forecast.market_collector import collect_market_snapshot

logger = logging.getLogger(__name__)


async def persist_public_market_snapshot(*, symbol: str = "BTCUSDT") -> bool:
    """Persist public OHLCV, funding, depth, trade-flow, OI and liquidation context.

    No language-model calls or paid ChatGPT forecast runs are made here.
    Existing websocket microdata and optional liquidation collectors run separately.
    """
    async with SessionLocal() as session:
        try:
            await collect_market_snapshot(session, symbol=symbol)
            await session.commit()
            return True
        except Exception:
            await session.rollback()
            logger.exception("Public market snapshot collection failed for %s", symbol)
            return False
