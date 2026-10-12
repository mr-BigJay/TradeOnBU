from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.candle_utils import candle_close_ms
from app.domain.time_utils import ensure_utc
from app.domain.candles import Candle
from app.models import Signal, SignalStatus
from app.services.execution_model import ExecutionModel, ExitOutcome
from app.services.market_data import MarketDataService
from app.services.signal_lifecycle import SignalLifecycleService


class SignalMonitorService:
    def __init__(
        self,
        market: MarketDataService,
        lifecycle: SignalLifecycleService,
        paper: ExecutionModel | None = None,
    ) -> None:
        self.market = market
        self.lifecycle = lifecycle
        self.paper = paper or ExecutionModel()

    def _monitor_from_ms(self, signal: Signal) -> int | None:
        """Earliest closed-bar open time eligible for entry/exit monitoring."""
        if signal.entry_monitor_from_ms is not None:
            return signal.entry_monitor_from_ms
        if not signal.telegram_approval_required:
            created = ensure_utc(signal.created_at) or datetime.now(timezone.utc)
            return int(created.timestamp() * 1000)
        if signal.telegram_approved_at:
            approved = ensure_utc(signal.telegram_approved_at)
            return int(approved.timestamp() * 1000) if approved else None
        return None

    def _eligible_closed_bars(self, signal: Signal, closed: list[Candle], interval: str = "5m") -> list[Candle]:
        cursor = signal.last_processed_candle_ms or 0
        monitor_from = self._monitor_from_ms(signal)
        bars = [b for b in closed if b.time_ms > cursor]
        if monitor_from is not None:
            bars = [b for b in bars if b.time_ms >= monitor_from]
        elif signal.telegram_approval_required:
            bars = []
        return sorted(bars, key=lambda b: b.time_ms)

    async def _resolve_bar_conflict(
        self,
        signal: Signal,
        bar: Candle,
    ) -> tuple[str | None, float | None, str | None]:
        status, raw_exit, note = self.lifecycle.resolve_active_bar(signal, bar.high, bar.low, bar.open)
        if not status or raw_exit is None:
            return None, None, None
        if note != "SL_FIRST_AMBIGUOUS":
            return status, raw_exit, note

        fine_bundle = await self.market.fetch_kline_bundle(signal.symbol, "1m", 30)
        window = [c for c in fine_bundle.closed if bar.time_ms <= c.time_ms < bar.time_ms + 5 * 60_000]
        for c in window:
            st, raw, nt = self.lifecycle.resolve_active_bar(signal, c.high, c.low, c.open)
            if st:
                return st, raw, nt or "RESOLVED_1M"
        return SignalStatus.SL_HIT.value, signal.stop_loss, "SL_FIRST_AMBIGUOUS"

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
                session, signal, "DATA_STALE_SKIP", signal.status, signal.status, note="Stale quote"
            )
            return events

        expires = ensure_utc(signal.pending_expires_at)
        if expires and now >= expires:
            await self.lifecycle.transition(session, signal, SignalStatus.EXPIRED.value, "PENDING_EXPIRED")
            events.append("EXPIRED")
        elif self.lifecycle.check_invalidation(signal, quote_price):
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

        if signal.telegram_approval_required and signal.telegram_approved is not True:
            return await self.process_waiting_unapproved(session, signal, quote_price, now, data_stale)

        if data_stale:
            await self.lifecycle.log_event(
                session, signal, "DATA_STALE_SKIP", signal.status, signal.status, note="Stale quote"
            )
            return events

        if signal.status == SignalStatus.DATA_ERROR.value:
            await self.lifecycle.transition(
                session, signal, SignalStatus.WAITING_FOR_ENTRY.value, "DATA_RECOVERED", note="Recovered"
            )

        expires = ensure_utc(signal.pending_expires_at)
        if expires and now >= expires:
            await self.lifecycle.transition(session, signal, SignalStatus.EXPIRED.value, "PENDING_EXPIRED")
            return ["EXPIRED"]

        if self.lifecycle.check_invalidation(signal, quote_price):
            await self.lifecycle.transition(session, signal, SignalStatus.INVALIDATED.value, "STRUCTURE_INVALIDATED")
            return ["INVALIDATED"]

        fav = self.lifecycle.favorable_move_pct(signal, quote_price)
        if fav >= max_missed_entry_pct:
            await self.lifecycle.transition(
                session,
                signal,
                SignalStatus.MISSED_ENTRY.value,
                "MISSED_ENTRY",
                payload={"favorable_move_pct": fav},
            )
            return ["MISSED_ENTRY"]

        for bar in bars:
            triggered, fill = self.lifecycle.entry_triggered(signal, bar.high, bar.low, bar.close)
            signal.last_processed_candle_ms = bar.time_ms
            if not triggered:
                continue
            close_ms = candle_close_ms(bar, "5m")
            signal.fill_price = fill
            signal.entry_activation_candle_ms = bar.time_ms
            signal.activated_at = datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc)
            signal.active_expires_at = signal.activated_at + timedelta(hours=signal.estimated_holding_hours)
            await self.lifecycle.transition(
                session,
                signal,
                SignalStatus.ACTIVE.value,
                "ENTRY_ACTIVATED",
                payload={"fill_price": fill, "candle_ms": bar.time_ms, "activation_ms": close_ms},
            )
            events.append("ACTIVE")
            return events
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
                session, signal, "DATA_STALE_SKIP", signal.status, signal.status, note="Stale quote"
            )
            return events

        if signal.status == SignalStatus.DATA_ERROR.value:
            await self.lifecycle.transition(session, signal, SignalStatus.ACTIVE.value, "DATA_RECOVERED", note="Recovered")

        activation_ms = signal.entry_activation_candle_ms or (
            int(signal.activated_at.timestamp() * 1000) if signal.activated_at else 0
        )
        bars = [b for b in bars if b.time_ms >= activation_ms]

        for bar in bars:
            signal.last_processed_candle_ms = bar.time_ms
            status, raw_exit, note = await self._resolve_bar_conflict(signal, bar)
            if not status or raw_exit is None:
                continue
            outcome = ExitOutcome.TP if status == SignalStatus.TP_HIT.value else ExitOutcome.SL
            fill = signal.fill_price or self.paper.open_fill(signal.entry_price, signal.direction, signal.entry_type)
            exit_fill = self.paper.close_fill(raw_exit, signal.direction, outcome)
            signal.exit_price = exit_fill
            signal.exit_reason = status
            signal.estimated_net_pnl_usdt = self.paper.net_pnl(signal.direction, fill, exit_fill)
            await self.lifecycle.transition(
                session,
                signal,
                status,
                status,
                payload={"raw_exit": raw_exit, "exit_fill": exit_fill, "candle_ms": bar.time_ms},
                note=note,
            )
            events.append(status)
            return events

        active_expires = ensure_utc(signal.active_expires_at)
        if active_expires and now >= active_expires:
            fill = signal.fill_price or self.paper.open_fill(signal.entry_price, signal.direction, signal.entry_type)
            exit_fill = self.paper.close_fill(quote_price, signal.direction, ExitOutcome.TIME)
            signal.exit_price = exit_fill
            signal.exit_reason = SignalStatus.TIME_EXIT.value
            signal.estimated_net_pnl_usdt = self.paper.net_pnl(signal.direction, fill, exit_fill)
            await self.lifecycle.transition(
                session,
                signal,
                SignalStatus.TIME_EXIT.value,
                "TIME_EXIT",
                payload={"raw_exit": quote_price, "exit_fill": exit_fill},
            )
            events.append("TIME_EXIT")
        return events

    async def monitor_open_signals(self, session: AsyncSession, settings_row) -> dict[str, list[str]]:
        result = await session.execute(
            select(Signal).where(
                Signal.status.in_(
                    [SignalStatus.WAITING_FOR_ENTRY.value, SignalStatus.ACTIVE.value, SignalStatus.DATA_ERROR.value]
                )
            )
        )
        signals = list(result.scalars().all())
        if not signals:
            return {}

        symbol = signals[0].symbol
        quote = await self.market.fetch_live_price(symbol)
        stale = self.market.is_quote_stale(quote)
        bundle = await self.market.fetch_kline_bundle(symbol, "5m", 200)
        history_ok = bundle.data_complete

        out: dict[str, list[str]] = {}
        for sig in signals:
            if not history_ok:
                await self.lifecycle.log_event(
                    session,
                    sig,
                    "INCOMPLETE_HISTORY_SKIP",
                    sig.status,
                    sig.status,
                    note="5m gap backfill incomplete — monitoring paused",
                )
                if sig.status != SignalStatus.DATA_ERROR.value:
                    await self.lifecycle.transition(
                        session,
                        sig,
                        SignalStatus.DATA_ERROR.value,
                        "INCOMPLETE_MARKET_DATA",
                        note="Gap backfill incomplete",
                    )
                continue
            bars = self._eligible_closed_bars(sig, bundle.closed, bundle.interval)
            if sig.status in (SignalStatus.WAITING_FOR_ENTRY.value, SignalStatus.DATA_ERROR.value):
                evs = await self.process_waiting(
                    session, sig, settings_row.max_missed_entry_pct, quote.price, bars, stale
                )
            else:
                evs = await self.process_active(session, sig, bars, quote.price, stale)
            if evs:
                out[sig.public_id] = evs
        await session.commit()
        return out
