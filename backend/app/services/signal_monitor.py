from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.candles import Candle
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

    def _signal_start_ms(self, signal: Signal) -> int:
        if signal.status == SignalStatus.ACTIVE.value and signal.activated_at:
            return int(signal.activated_at.timestamp() * 1000)
        return int(signal.created_at.timestamp() * 1000)

    def _pending_bars(
        self,
        signal: Signal,
        candles: list[Candle],
    ) -> list[Candle]:
        start_ms = self._signal_start_ms(signal)
        cursor = signal.last_processed_candle_ms or 0
        return [c for c in candles if c.time_ms > cursor and c.time_ms >= start_ms]

    async def _resolve_bar_conflict(
        self,
        signal: Signal,
        bar: Candle,
    ) -> tuple[str | None, float | None, str | None]:
        status, exit_price, note = self.lifecycle.resolve_active_bar(
            signal, bar.high, bar.low, bar.open
        )
        if status and (note is None or "CONSERVATIVE" not in (note or "")):
            return status, exit_price, note

        if status and note and "CONSERVATIVE" in note:
            fine = await self.market.fetch_klines(signal.symbol, "1m", 10)
            window = [c for c in fine if bar.time_ms <= c.time_ms < bar.time_ms + 5 * 60_000]
            for c in window:
                st, ex, nt = self.lifecycle.resolve_active_bar(signal, c.high, c.low, c.open)
                if st:
                    return st, ex, nt or "RESOLVED_1M"
            return SignalStatus.SL_HIT.value, exit_price, note
        return status, exit_price, note

    async def process_waiting_unapproved(
        self,
        session: AsyncSession,
        signal: Signal,
        quote_price: float,
        now: datetime,
        data_stale: bool,
    ) -> list[str]:
        events: list[str] = []
        if data_stale:
            await self.lifecycle.log_event(
                session,
                signal,
                "DATA_STALE_SKIP",
                signal.status,
                signal.status,
                note="Stale data; pending lifecycle checks paused",
            )
            return events

        if signal.pending_expires_at and now >= signal.pending_expires_at:
            await self.lifecycle.transition(session, signal, SignalStatus.EXPIRED.value, "PENDING_EXPIRED")
            events.append("EXPIRED")
            return events

        if self.lifecycle.check_invalidation(signal, quote_price):
            await self.lifecycle.transition(session, signal, SignalStatus.INVALIDATED.value, "STRUCTURE_INVALIDATED")
            events.append("INVALIDATED")
        return events

    async def process_waiting(
        self,
        session: AsyncSession,
        signal: Signal,
        max_missed_entry_pct: float,
        quote_price: float,
        bars: list[Candle],
        data_stale: bool,
    ) -> list[str]:
        events: list[str] = []
        now = datetime.now(timezone.utc)

        approved = not signal.telegram_approval_required or signal.telegram_approved is True
        if not approved:
            ev = await self.process_waiting_unapproved(session, signal, quote_price, now, data_stale)
            if bars:
                signal.last_processed_candle_ms = max(signal.last_processed_candle_ms, bars[-1].time_ms)
            return ev

        if data_stale:
            await self.lifecycle.log_event(
                session,
                signal,
                "DATA_STALE_SKIP",
                signal.status,
                signal.status,
                note="Stale data; entry monitoring skipped",
            )
            return events

        if signal.status == SignalStatus.DATA_ERROR.value:
            await self.lifecycle.transition(
                session,
                signal,
                SignalStatus.WAITING_FOR_ENTRY.value,
                "DATA_RECOVERED",
                note="Resumed waiting after recoverable data error",
            )

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

        for bar in bars:
            triggered, fill = self.lifecycle.entry_triggered(signal, bar.high, bar.low, bar.close)
            if triggered:
                signal.fill_price = fill
                signal.activated_at = now
                signal.active_expires_at = now + timedelta(hours=signal.estimated_holding_hours)
                await self.lifecycle.transition(
                    session,
                    signal,
                    SignalStatus.ACTIVE.value,
                    "ENTRY_ACTIVATED",
                    payload={"fill_price": fill, "candle_ms": bar.time_ms},
                )
                events.append("ACTIVE")
                signal.last_processed_candle_ms = bar.time_ms
                return events
            signal.last_processed_candle_ms = bar.time_ms
        return events

    async def process_active(
        self,
        session: AsyncSession,
        signal: Signal,
        bars: list[Candle],
        quote_price: float,
        data_stale: bool,
    ) -> list[str]:
        events: list[str] = []
        now = datetime.now(timezone.utc)

        if data_stale:
            await self.lifecycle.log_event(
                session,
                signal,
                "DATA_STALE_SKIP",
                signal.status,
                signal.status,
                note="Stale data; active monitoring skipped",
            )
            return events

        if signal.status == SignalStatus.DATA_ERROR.value:
            await self.lifecycle.transition(
                session,
                signal,
                SignalStatus.ACTIVE.value,
                "DATA_RECOVERED",
                note="Resumed active monitoring after recoverable data error",
            )

        for bar in bars:
            status, exit_price, note = await self._resolve_bar_conflict(signal, bar)
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
                    payload={"exit_price": exit_price, "candle_ms": bar.time_ms},
                    note=note,
                )
                events.append(status)
                signal.last_processed_candle_ms = bar.time_ms
                return events
            signal.last_processed_candle_ms = bar.time_ms

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
                Signal.status.in_(
                    [
                        SignalStatus.WAITING_FOR_ENTRY.value,
                        SignalStatus.ACTIVE.value,
                        SignalStatus.DATA_ERROR.value,
                    ]
                )
            )
        )
        signals = list(result.scalars().all())
        if not signals:
            return {}

        symbols = {s.symbol for s in signals}
        symbol = next(iter(symbols))
        quote = await self.market.fetch_live_price(symbol)
        stale = self.market.is_quote_stale(quote)
        candles = await self.market.fetch_klines(symbol, "5m", 120)
        confirmed = candles[:-1] if len(candles) > 1 else candles

        out: dict[str, list[str]] = {}
        for sig in signals:
            bars = self._pending_bars(sig, confirmed)
            if sig.status in (SignalStatus.WAITING_FOR_ENTRY.value, SignalStatus.DATA_ERROR.value) and (
                sig.status != SignalStatus.DATA_ERROR.value or sig.telegram_approved
            ):
                if sig.status == SignalStatus.DATA_ERROR.value and not sig.telegram_approved:
                    evs = await self.process_waiting_unapproved(
                        session, sig, quote.price, datetime.now(timezone.utc), stale
                    )
                else:
                    evs = await self.process_waiting(
                        session,
                        sig,
                        settings_row.max_missed_entry_pct,
                        quote.price,
                        bars,
                        stale,
                    )
            elif sig.status == SignalStatus.ACTIVE.value or (
                sig.status == SignalStatus.DATA_ERROR.value and sig.activated_at
            ):
                active_bars = [
                    b
                    for b in bars
                    if sig.activated_at and b.time_ms >= int(sig.activated_at.timestamp() * 1000)
                ]
                evs = await self.process_active(session, sig, active_bars, quote.price, stale)
            else:
                evs = []
            if evs:
                out[sig.public_id] = evs
        await session.commit()
        return out
