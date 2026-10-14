from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.candles import Candle
from app.models import SimPosition
from app.services.market_data import MarketDataService
from app.services.position.position_service import SimPositionService
from app.services.position.settings_service import get_or_create_settings


def _bar_hits_long_limit(bar: Candle, limit: float) -> bool:
    return bar.low <= limit <= bar.high


def _bar_hits_short_limit(bar: Candle, limit: float) -> bool:
    return bar.low <= limit <= bar.high


def _tp_sl_on_bar(direction: str, bar: Candle, sl: float, tp: float) -> tuple[str | None, float | None, bool]:
    """Returns (reason, exit_price, ambiguous)."""
    if direction == "LONG":
        sl_hit = bar.low <= sl
        tp_hit = bar.high >= tp
        if sl_hit and tp_hit:
            return None, None, True
        if sl_hit:
            return "sl_hit", sl, False
        if tp_hit:
            return "tp_hit", tp, False
    else:
        sl_hit = bar.high >= sl
        tp_hit = bar.low <= tp
        if sl_hit and tp_hit:
            return None, None, True
        if sl_hit:
            return "sl_hit", sl, False
        if tp_hit:
            return "tp_hit", tp, False
    return None, None, False


def _liquidation_on_bar(direction: str, bar: Candle, liq: float | None) -> bool:
    if liq is None:
        return False
    if direction == "LONG":
        return bar.low <= liq
    return bar.high >= liq


class SimPositionMonitorService:
    def __init__(self, market: MarketDataService | None = None) -> None:
        self.market = market or MarketDataService()
        self.positions = SimPositionService(self.market)

    def _eligible_bars(self, pos: SimPosition, closed: list[Candle]) -> list[Candle]:
        cursor = pos.last_processed_candle_ms or 0
        monitor_from = pos.monitor_from_ms or 0
        bars = [b for b in closed if b.time_ms > cursor and b.time_ms >= monitor_from]
        return sorted(bars, key=lambda b: b.time_ms)

    async def monitor_session(self, session: AsyncSession) -> int:
        settings = await get_or_create_settings(session)
        result = await session.execute(
            select(SimPosition).where(SimPosition.status.in_(("pending_entry", "open")))
        )
        positions = list(result.scalars().all())
        if not positions:
            return 0
        symbol = positions[0].symbol
        bundle = await self.market.fetch_kline_bundle(symbol, "1m", 120)
        closed = bundle.closed
        processed = 0

        from app.services.position.calculations import estimate_isolated_liquidation

        for pos in positions:
            bars = self._eligible_bars(pos, closed)
            for bar in bars:
                if pos.status == "pending_entry" and pos.order_type == "limit" and pos.limit_entry_price:
                    limit = pos.limit_entry_price
                    filled = _bar_hits_long_limit(bar, limit) if pos.direction == "LONG" else _bar_hits_short_limit(bar, limit)
                    if filled:
                        await self.positions._fill_entry(
                            session, pos, settings, limit, transition_suffix=f"limit:{bar.time_ms}"
                        )
                        pos.last_processed_candle_ms = bar.time_ms
                        processed += 1
                        continue

                if pos.status == "open":
                    entry = pos.fill_price or pos.entry_price or 0.0
                    liq, _ = estimate_isolated_liquidation(pos.direction, entry, pos.leverage)
                    if _liquidation_on_bar(pos.direction, bar, liq):
                        await self.positions._close_position(
                            session,
                            pos,
                            settings,
                            raw_exit=liq or bar.close,
                            reason="liquidated",
                            exit_adverse=True,
                            bar_ms=bar.time_ms,
                        )
                        pos.last_processed_candle_ms = bar.time_ms
                        processed += 1
                        break

                    reason, raw_exit, ambiguous = _tp_sl_on_bar(pos.direction, bar, pos.stop_loss, pos.take_profit)
                    if ambiguous:
                        await self.positions._close_position(
                            session,
                            pos,
                            settings,
                            raw_exit=bar.close,
                            reason="sl_hit",
                            exit_adverse=True,
                            bar_ms=bar.time_ms,
                            ambiguous=True,
                        )
                        pos.last_processed_candle_ms = bar.time_ms
                        processed += 1
                        break
                    if reason and raw_exit is not None:
                        await self.positions._close_position(
                            session,
                            pos,
                            settings,
                            raw_exit=raw_exit,
                            reason=reason,
                            exit_adverse=reason == "sl_hit",
                            bar_ms=bar.time_ms,
                        )
                        pos.last_processed_candle_ms = bar.time_ms
                        processed += 1
                        break

                pos.last_processed_candle_ms = bar.time_ms

        if processed:
            await session.commit()
        return processed
