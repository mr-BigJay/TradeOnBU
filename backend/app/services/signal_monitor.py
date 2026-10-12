from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.candle_utils import candle_close_ms, interval_ms
from app.domain.time_utils import ensure_utc
from app.domain.candles import Candle
from app.models import Signal, SignalStatus
from app.services.execution_model import ExecutionModel, ExitOutcome
from app.services.market_data import MarketDataService
from app.services.signal_lifecycle import SignalLifecycleService

MONITORABLE = frozenset(
    {
        SignalStatus.WAITING_FOR_ENTRY.value,
        SignalStatus.ACTIVE.value,
        SignalStatus.DATA_ERROR.value,
    }
)


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
        if signal.entry_monitor_from_ms is not None:
            return signal.entry_monitor_from_ms
        if not signal.telegram_approval_required:
            created = ensure_utc(signal.created_at) or datetime.now(timezone.utc)
            return int(created.timestamp() * 1000)
        if signal.telegram_approved_at:
            approved = ensure_utc(signal.telegram_approved_at)
            return int(approved.timestamp() * 1000) if approved else None
        return None

    def _prior_status(self, signal: Signal) -> str:
        if signal.status != SignalStatus.DATA_ERROR.value:
            return signal.status
        prior = signal.analysis.get("pre_data_error_status")
        if prior in (SignalStatus.WAITING_FOR_ENTRY.value, SignalStatus.ACTIVE.value):
            return prior
        return SignalStatus.WAITING_FOR_ENTRY.value

    async def _enter_data_error(self, session: AsyncSession, signal: Signal, note: str) -> None:
        if signal.status == SignalStatus.DATA_ERROR.value:
            return
        signal.analysis = {
            **signal.analysis,
            "pre_data_error_status": signal.status,
        }
        await self.lifecycle.transition(
            session,
            signal,
            SignalStatus.DATA_ERROR.value,
            "INCOMPLETE_MARKET_DATA",
            note=note,
        )

    async def _recover_data_error(self, session: AsyncSession, signal: Signal) -> None:
        if signal.status != SignalStatus.DATA_ERROR.value:
            return
        prior = self._prior_status(signal)
        await self.lifecycle.transition(session, signal, prior, "DATA_RECOVERED", note=f"Resumed {prior}")

    def _eligible_closed_bars(self, signal: Signal, closed: list[Candle], interval: str = "5m") -> list[Candle]:
        cursor = signal.last_processed_candle_ms or 0
        monitor_from = self._monitor_from_ms(signal)
        bars = [b for b in closed if b.time_ms > cursor]
        if monitor_from is not None:
            bars = [b for b in bars if b.time_ms >= monitor_from]
        elif signal.telegram_approval_required:
            bars = []
        return sorted(bars, key=lambda b: b.time_ms)

    async def _finalize_exit(
        self,
        session: AsyncSession,
        signal: Signal,
        status: str,
        raw_exit: float,
        note: str | None,
        bar_ms: int,
        events: list[str],
    ) -> list[str]:
        outcome = ExitOutcome.TP if status == SignalStatus.TP_HIT.value else ExitOutcome.SL
        paper = ExecutionModel.for_signal(signal)
        fill = signal.fill_price or paper.open_fill(signal.entry_price, signal.direction, signal.entry_type)
        exit_fill = paper.close_fill(raw_exit, signal.direction, outcome)
        signal.exit_price = exit_fill
        signal.exit_reason = status
        signal.estimated_net_pnl_usdt = paper.net_pnl(signal.direction, fill, exit_fill)
        await self.lifecycle.transition(
            session,
            signal,
            status,
            status,
            payload={"raw_exit": raw_exit, "exit_fill": exit_fill, "candle_ms": bar_ms},
            note=note,
        )
        events.append(status)
        return events

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

    async def _try_entry_and_same_bar_exit(
        self,
        session: AsyncSession,
        signal: Signal,
        bar: Candle,
    ) -> list[str]:
        """Walk 1m candles inside a 5m bar: entry then post-entry TP/SL on the same 5m bar."""
        events: list[str] = []
        step_5m = interval_ms("5m")
        m1_bundle = await self.market.fetch_kline_bundle(signal.symbol, "1m", 120)
        window = sorted(
            [c for c in m1_bundle.closed if bar.time_ms <= c.time_ms < bar.time_ms + step_5m],
            key=lambda c: c.time_ms,
        )

        if window:
            entered = False
            for c in window:
                if not entered:
                    triggered, fill = self.lifecycle.entry_triggered(signal, c.high, c.low, c.close)
                    if not triggered:
                        continue
                    entered = True
                    close_ms = c.time_ms + interval_ms("1m")
                    signal.fill_price = fill
                    signal.entry_activation_candle_ms = c.time_ms
                    signal.activated_at = datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc)
                    signal.active_expires_at = signal.activated_at + timedelta(hours=signal.estimated_holding_hours)
                    await self.lifecycle.transition(
                        session,
                        signal,
                        SignalStatus.ACTIVE.value,
                        "ENTRY_ACTIVATED",
                        payload={"fill_price": fill, "candle_ms": c.time_ms, "activation_ms": close_ms, "via": "1m"},
                    )
                    events.append("ACTIVE")
                    status, raw_exit, note = await self._resolve_bar_conflict(signal, c)
                    if status and raw_exit is not None:
                        return await self._finalize_exit(session, signal, status, raw_exit, note, c.time_ms, events)
                    continue
                status, raw_exit, note = await self._resolve_bar_conflict(signal, c)
                if status and raw_exit is not None:
                    return await self._finalize_exit(session, signal, status, raw_exit, note, c.time_ms, events)
            signal.last_processed_candle_ms = bar.time_ms
            return events if entered else []

        triggered, fill = self.lifecycle.entry_triggered(signal, bar.high, bar.low, bar.close)
        if not triggered:
            return []
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
            payload={"fill_price": fill, "candle_ms": bar.time_ms, "activation_ms": close_ms, "via": "5m"},
        )
        events.append("ACTIVE")
        status, raw_exit, note = await self._resolve_bar_conflict(signal, bar)
        if status and raw_exit is not None:
            return await self._finalize_exit(session, signal, status, raw_exit, note, bar.time_ms, events)
        signal.last_processed_candle_ms = bar.time_ms
        return events

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

        if signal.status == SignalStatus.ACTIVE.value or signal.fill_price is not None:
            return events

        if data_stale:
            await self.lifecycle.log_event(
                session, signal, "DATA_STALE_SKIP", signal.status, signal.status, note="Stale quote"
            )
            return events

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
            bar_events = await self._try_entry_and_same_bar_exit(session, signal, bar)
            if bar_events:
                return bar_events
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
                session, signal, "DATA_STALE_SKIP", signal.status, signal.status, note="Stale quote"
            )
            return events

        activation_ms = signal.entry_activation_candle_ms or (
            int(signal.activated_at.timestamp() * 1000) if signal.activated_at else 0
        )
        bars = [b for b in bars if b.time_ms >= activation_ms]

        for bar in bars:
            signal.last_processed_candle_ms = bar.time_ms
            status, raw_exit, note = await self._resolve_bar_conflict(signal, bar)
            if not status or raw_exit is None:
                continue
            return await self._finalize_exit(session, signal, status, raw_exit, note, bar.time_ms, events)

        active_expires = ensure_utc(signal.active_expires_at)
        if active_expires and now >= active_expires:
            paper = ExecutionModel.for_signal(signal)
            fill = signal.fill_price or paper.open_fill(signal.entry_price, signal.direction, signal.entry_type)
            exit_fill = paper.close_fill(quote_price, signal.direction, ExitOutcome.TIME)
            signal.exit_price = exit_fill
            signal.exit_reason = SignalStatus.TIME_EXIT.value
            signal.estimated_net_pnl_usdt = paper.net_pnl(signal.direction, fill, exit_fill)
            await self.lifecycle.transition(
                session,
                signal,
                SignalStatus.TIME_EXIT.value,
                "TIME_EXIT",
                payload={"raw_exit": quote_price, "exit_fill": exit_fill},
            )
            events.append("TIME_EXIT")
        return events

    async def _history_from_ms(self, signal: Signal) -> int:
        cursor = signal.last_processed_candle_ms or 0
        monitor_from = self._monitor_from_ms(signal) or 0
        return max(cursor, monitor_from)

    async def monitor_open_signals(self, session: AsyncSession, settings_row) -> dict[str, list[str]]:
        result = await session.execute(select(Signal).where(Signal.status.in_(MONITORABLE)))
        signals = list(result.scalars().all())
        if not signals:
            return {}

        symbol = signals[0].symbol
        quote = await self.market.fetch_live_price(symbol)
        stale = self.market.is_quote_stale(quote)

        out: dict[str, list[str]] = {}
        for sig in signals:
            from_ms = await self._history_from_ms(sig)
            bundle = await self.market.fetch_historical_closed(sig.symbol, "5m", from_ms)
            if not bundle.data_complete:
                await self.lifecycle.log_event(
                    session,
                    sig,
                    "INCOMPLETE_HISTORY_SKIP",
                    sig.status,
                    sig.status,
                    note=f"Cannot reconstruct 5m history from {from_ms}",
                )
                await self._enter_data_error(session, sig, "Gap backfill incomplete for signal cursor")
                continue

            if sig.status == SignalStatus.DATA_ERROR.value:
                await self._recover_data_error(session, sig)

            bars = self._eligible_closed_bars(sig, bundle.closed, bundle.interval)
            if sig.status == SignalStatus.ACTIVE.value:
                evs = await self.process_active(session, sig, bars, quote.price, stale)
            else:
                evs = await self.process_waiting(
                    session, sig, settings_row.max_missed_entry_pct, quote.price, bars, stale
                )
            if evs:
                out[sig.public_id] = evs
        await session.commit()
        return out
