from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.candles import Candle
from app.models import SimPosition
from app.services.market_data import MarketDataService
from app.services.position.equity_service import record_equity_snapshot
from app.services.position.position_service import SimPositionService
from app.services.position.settings_service import get_or_create_settings

MAX_RECOVERY_MINUTES = 24 * 60


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

    def _kline_limit_for_positions(self, positions: list[SimPosition]) -> int:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        worst_gap_min = 120
        for pos in positions:
            cursor = pos.last_processed_candle_ms or pos.monitor_from_ms or now_ms
            gap_min = max(0, (now_ms - cursor) // 60000)
            worst_gap_min = max(worst_gap_min, int(gap_min) + 2)
        return min(worst_gap_min + 5, MAX_RECOVERY_MINUTES)

    async def monitor_session(self, session: AsyncSession) -> int:
        settings = await get_or_create_settings(session)
        result = await session.execute(
            select(SimPosition).where(SimPosition.status.in_(("pending_entry", "open")))
        )
        positions = list(result.scalars().all())
        if not positions:
            return 0
        symbol = positions[0].symbol
        limit = self._kline_limit_for_positions(positions)
        bundle = await self.market.fetch_kline_bundle(symbol, "1m", limit)
        closed = bundle.closed
        if closed:
            oldest_ms = closed[0].time_ms
            for pos in positions:
                need_from = pos.last_processed_candle_ms or pos.monitor_from_ms or 0
                if need_from and need_from < oldest_ms:
                    gaps = list(pos.extra.get("monitor_gaps") or [])
                    gaps.append(
                        {
                            "from_ms": need_from,
                            "oldest_available_ms": oldest_ms,
                            "noted_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                    pos.extra = {**pos.extra, "monitor_gaps": gaps[-20:]}

        processed = 0

        from app.services.position.calculations import estimate_isolated_liquidation

        for pos in positions:
            bars = self._eligible_bars(pos, closed)
            for bar in bars:
                if pos.status == "pending_entry" and pos.order_type == "limit" and pos.limit_entry_price:
                    limit_px = pos.limit_entry_price
                    filled = (
                        _bar_hits_long_limit(bar, limit_px)
                        if pos.direction == "LONG"
                        else _bar_hits_short_limit(bar, limit_px)
                    )
                    if filled:
                        await self.positions._fill_entry(
                            session, pos, settings, limit_px, transition_suffix=f"limit:{bar.time_ms}"
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
                            reason="ambiguous_intrabar",
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

        unrealized = 0.0
        for pos in positions:
            if pos.status != "open":
                continue
            data = await self.positions.enrich_open(session, pos)
            metrics = data.get("metrics") or {}
            unrealized += float(metrics.get("net_unrealized_pnl") or 0)
        await record_equity_snapshot(session, unrealized_pnl=unrealized)
        if processed or positions:
            await session.commit()
        return processed
