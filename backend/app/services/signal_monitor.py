from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Signal, SignalStatus
from app.services.market_data import MarketDataService
from app.services.paper_execution import PaperExecutionEngine
from app.services.signal_lifecycle import SignalLifecycleService


class SignalMonitorService:
    def __init__(
        self,
        market: MarketDataService,
        lifecycle: SignalLifecycleService,
        paper: PaperExecutionEngine | None = None,
    ) -> None:
        self.market = market
        self.lifecycle = lifecycle
        self.paper = paper or PaperExecutionEngine()

    async def process_waiting(
        self,
        session: AsyncSession,
        signal: Signal,
        max_missed_entry_pct: float,
        quote_price: float,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        data_stale: bool,
    ) -> list[str]:
        events: list[str] = []
        now = datetime.now(timezone.utc)

        if signal.telegram_approval_required and signal.telegram_approved is not True:
            return events

        if data_stale:
            await self.lifecycle.transition(
                session,
                signal,
                SignalStatus.DATA_ERROR.value,
                "DATA_STALE",
                note="Market data stale while waiting for entry",
            )
            events.append("DATA_ERROR")
            return events

        if signal.pending_expires_at and now >= signal.pending_expires_at:
            await self.lifecycle.transition(session, signal, SignalStatus.EXPIRED.value, "PENDING_EXPIRED")
            events.append("EXPIRED")
            return events

        if self.lifecycle.check_invalidation(signal, quote_price):
            await self.lifecycle.transition(session, signal, SignalStatus.INVALIDATED.value, "STRUCTURE_INVALIDATED")
            events.append("INVALIDATED")
            return events

        fav = self.lifecycle.favorable_move_pct(signal, quote_price)
        if fav >= max_missed_entry_pct:
            await self.lifecycle.transition(
                session,
                signal,
                SignalStatus.MISSED_ENTRY.value,
                "MISSED_ENTRY",
                payload={"favorable_move_pct": fav},
            )
            events.append("MISSED_ENTRY")
            return events

        triggered, fill = self.lifecycle.entry_triggered(signal, bar_high, bar_low, bar_close)
        if triggered:
            signal.fill_price = fill
            signal.activated_at = now
            if signal.estimated_holding_hours:
                from datetime import timedelta

                signal.active_expires_at = now + timedelta(hours=signal.estimated_holding_hours)
            await self.lifecycle.transition(
                session,
                signal,
                SignalStatus.ACTIVE.value,
                "ENTRY_ACTIVATED",
                payload={"fill_price": fill},
            )
            events.append("ACTIVE")
        return events

    async def process_active(
        self,
        session: AsyncSession,
        signal: Signal,
        bar_high: float,
        bar_low: float,
        bar_open: float,
        quote_price: float,
        data_stale: bool,
    ) -> list[str]:
        events: list[str] = []
        now = datetime.now(timezone.utc)

        if data_stale:
            await self.lifecycle.transition(
                session,
                signal,
                SignalStatus.DATA_ERROR.value,
                "DATA_STALE",
                note="Market data stale while active",
            )
            events.append("DATA_ERROR")
            return events

        status, exit_price, note = self.lifecycle.resolve_active_bar(signal, bar_high, bar_low, bar_open)
        if status and exit_price is not None:
            signal.exit_price = exit_price
            signal.exit_reason = status
            fill = signal.fill_price or signal.entry_price
            signal.estimated_net_pnl_usdt = self.paper.pnl_usdt(signal.direction, fill, exit_price)
            await self.lifecycle.transition(
                session,
                signal,
                status,
                status,
                payload={"exit_price": exit_price},
                note=note,
            )
            events.append(status)
            return events

        if signal.active_expires_at and now >= signal.active_expires_at:
            exit_price = self.paper.simulate_exit(quote_price, signal.direction, hit_tp=False)
            signal.exit_price = exit_price
            signal.exit_reason = SignalStatus.TIME_EXIT.value
            fill = signal.fill_price or signal.entry_price
            signal.estimated_net_pnl_usdt = self.paper.pnl_usdt(signal.direction, fill, exit_price)
            await self.lifecycle.transition(
                session,
                signal,
                SignalStatus.TIME_EXIT.value,
                "TIME_EXIT",
                payload={"exit_price": exit_price},
            )
            events.append("TIME_EXIT")
        return events

    async def monitor_open_signals(self, session: AsyncSession, settings_row) -> dict[str, list[str]]:
        result = await session.execute(
            select(Signal).where(
                Signal.status.in_([SignalStatus.WAITING_FOR_ENTRY.value, SignalStatus.ACTIVE.value])
            )
        )
        signals = list(result.scalars().all())
        if not signals:
            return {}

        symbol = signals[0].symbol
        quote = await self.market.fetch_live_price(symbol)
        stale = self.market.is_quote_stale(quote)
        candles = await self.market.fetch_klines(symbol, "5m", 3)
        bar = candles[-2] if len(candles) >= 2 else candles[-1]

        out: dict[str, list[str]] = {}
        for sig in signals:
            if sig.status == SignalStatus.WAITING_FOR_ENTRY.value:
                evs = await self.process_waiting(
                    session,
                    sig,
                    settings_row.max_missed_entry_pct,
                    quote.price,
                    bar.high,
                    bar.low,
                    bar.close,
                    stale,
                )
            else:
                evs = await self.process_active(session, sig, bar.high, bar.low, bar.open, quote.price, stale)
            if evs:
                out[sig.public_id] = evs
        await session.commit()
        return out
