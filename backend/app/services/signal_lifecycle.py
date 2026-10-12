from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.state_machine import TERMINAL_STATUSES, can_transition
from app.models import Signal, SignalEvent, SignalStatus
from app.services.entry_detection import SetupCandidate
from app.services.execution_model import ExecutionModel


class SignalLifecycleService:
    def __init__(self, paper: ExecutionModel | None = None) -> None:
        self.paper = paper or ExecutionModel()

    async def log_event(
        self,
        session: AsyncSession,
        signal: Signal,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        payload: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> SignalEvent:
        ev = SignalEvent(
            signal_id=signal.id,
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            payload=payload or {},
            note=note,
        )
        session.add(ev)
        return ev

    async def transition(
        self,
        session: AsyncSession,
        signal: Signal,
        new_status: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> bool:
        if signal.status in TERMINAL_STATUSES:
            return False
        if not can_transition(signal.status, new_status):
            return False
        old = signal.status
        signal.status = new_status
        signal.updated_at = datetime.now(timezone.utc)
        if new_status in TERMINAL_STATUSES:
            signal.closed_at = datetime.now(timezone.utc)
        await self.log_event(session, signal, event_type, old, new_status, payload, note)
        return True

    async def create_from_candidate(
        self,
        session: AsyncSession,
        candidate: SetupCandidate,
        max_pending_minutes: int,
        require_telegram: bool,
        holding_time_hours: float = 6.0,
    ) -> Signal | None:
        if candidate.rejected_reason:
            return None
        existing = await session.execute(
            select(Signal).where(
                Signal.dedupe_key == candidate.dedupe_key,
                Signal.status.notin_(list(TERMINAL_STATUSES)),
            )
        )
        if existing.scalar_one_or_none():
            return None

        from app.config import get_effective_settings

        eff = get_effective_settings()
        now = datetime.now(timezone.utc)
        position_snapshot = {
            "margin_usdt": eff.margin_usdt,
            "leverage": eff.leverage,
            "maker_fee_rate": eff.maker_fee_rate,
            "taker_fee_rate": eff.taker_fee_rate,
            "spread_bps": eff.spread_bps,
            "slippage_bps": eff.slippage_bps,
        }
        merged_analysis = {**(candidate.analysis or {}), "position_params": position_snapshot}
        sig = Signal(
            public_id=str(uuid4()),
            symbol=candidate.symbol,
            status=SignalStatus.CREATED.value,
            direction=candidate.direction,
            entry_type=candidate.entry_type,
            entry_price=candidate.entry_price,
            entry_zone_low=candidate.entry_zone_low,
            entry_zone_high=candidate.entry_zone_high,
            stop_loss=candidate.stop_loss,
            take_profit=candidate.take_profit,
            invalidation_level=candidate.invalidation_level,
            market_bias=candidate.market_bias,
            liquidity_sweep=candidate.liquidity_sweep,
            mss_confirmation=candidate.mss_confirmation,
            fvg_entry=candidate.fvg_entry,
            gross_rr=candidate.gross_rr,
            net_rr=candidate.net_rr,
            estimated_holding_hours=holding_time_hours,
            analysis=merged_analysis,
            dedupe_key=candidate.dedupe_key,
            telegram_approval_required=require_telegram,
            telegram_approved=None if require_telegram else True,
            pending_expires_at=now + timedelta(minutes=max_pending_minutes),
        )
        session.add(sig)
        await session.flush()
        await self.log_event(session, sig, "SIGNAL_CREATED", None, SignalStatus.CREATED.value, {"candidate": candidate.analysis})
        await self.transition(session, sig, SignalStatus.WAITING_FOR_ENTRY.value, "ENTER_WAITING")
        return sig

    def favorable_move_pct(self, signal: Signal, price: float) -> float:
        if signal.direction == "LONG":
            return (price - signal.entry_price) / signal.entry_price * 100
        return (signal.entry_price - price) / signal.entry_price * 100

    def entry_triggered(self, signal: Signal, candle_high: float, candle_low: float, candle_close: float) -> tuple[bool, float]:
        entry = signal.entry_price
        et = signal.entry_type
        fill_raw: float | None = None
        if signal.direction == "LONG":
            if et == "LIMIT":
                fill_raw = entry if candle_low <= entry else None
            elif et == "STOP":
                fill_raw = entry if candle_high >= entry else None
            else:
                fill_raw = candle_close if candle_close >= entry and candle_low <= entry else None
        else:
            if et == "LIMIT":
                fill_raw = entry if candle_high >= entry else None
            elif et == "STOP":
                fill_raw = entry if candle_low <= entry else None
            else:
                fill_raw = candle_close if candle_close <= entry and candle_high >= entry else None
        if fill_raw is None:
            return False, 0.0
        fill = self.paper.open_fill(fill_raw, signal.direction, et)
        return True, fill

    def check_invalidation(self, signal: Signal, price: float) -> bool:
        if signal.direction == "LONG":
            return price < signal.invalidation_level
        return price > signal.invalidation_level

    def resolve_active_bar(
        self,
        signal: Signal,
        bar_high: float,
        bar_low: float,
        bar_open: float,
    ) -> tuple[str | None, float | None, str | None]:
        if signal.direction == "LONG":
            hit_sl = bar_low <= signal.stop_loss
            hit_tp = bar_high >= signal.take_profit
            if hit_sl and hit_tp:
                return SignalStatus.SL_HIT.value, signal.stop_loss, "SL_FIRST_AMBIGUOUS"
            if hit_sl:
                return SignalStatus.SL_HIT.value, signal.stop_loss, None
            if hit_tp:
                return SignalStatus.TP_HIT.value, signal.take_profit, None
        else:
            hit_sl = bar_high >= signal.stop_loss
            hit_tp = bar_low <= signal.take_profit
            if hit_sl and hit_tp:
                return SignalStatus.SL_HIT.value, signal.stop_loss, "SL_FIRST_AMBIGUOUS"
            if hit_sl:
                return SignalStatus.SL_HIT.value, signal.stop_loss, None
            if hit_tp:
                return SignalStatus.TP_HIT.value, signal.take_profit, None
        return None, None, None
