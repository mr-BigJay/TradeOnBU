from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.domain.time_utils import ensure_utc
from app.domain.state_machine import TERMINAL_STATUSES
from app.models import Signal, SignalStatus
from app.services.signal_lifecycle import SignalLifecycleService


APPROVAL_ALLOWED_STATUSES = frozenset({SignalStatus.WAITING_FOR_ENTRY.value, SignalStatus.DATA_ERROR.value})


class TelegramApprovalService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.lifecycle = SignalLifecycleService()

    def user_allowed(self, user_id: int, source: str = "telegram") -> bool:
        if source != "telegram":
            return True
        allow = self.settings.allowed_telegram_users()
        if not allow:
            return False
        return user_id in allow

    async def _get_signal_for_update(self, session: AsyncSession, public_id: str) -> Signal | None:
        result = await session.execute(select(Signal).where(Signal.public_id == public_id).with_for_update())
        return result.scalar_one_or_none()

    def _entry_touched_on_bar(self, signal: Signal, bar_high: float, bar_low: float, bar_close: float) -> bool:
        triggered, _ = self.lifecycle.entry_triggered(signal, bar_high, bar_low, bar_close)
        return triggered

    async def approve(
        self,
        session: AsyncSession,
        public_id: str,
        user_id: int,
        source: str = "telegram",
        pre_approval_bars: list | None = None,
    ) -> tuple[Signal | None, str | None]:
        if not self.user_allowed(user_id, source):
            return None, "UNAUTHORIZED_USER"

        sig = await self._get_signal_for_update(session, public_id)
        if not sig:
            return None, "NOT_FOUND"

        if sig.status in TERMINAL_STATUSES or sig.status == SignalStatus.ACTIVE.value:
            return sig, "SIGNAL_NOT_APPROVABLE"

        if sig.status not in APPROVAL_ALLOWED_STATUSES:
            return sig, "SIGNAL_NOT_APPROVABLE"

        if sig.telegram_approved is True:
            return sig, "ALREADY_APPROVED"

        now = datetime.now(timezone.utc)
        expires = ensure_utc(sig.pending_expires_at)
        if expires and now >= expires:
            await self.lifecycle.transition(session, sig, SignalStatus.EXPIRED.value, "PENDING_EXPIRED")
            await session.commit()
            return sig, "SIGNAL_EXPIRED"

        if pre_approval_bars:
            for bar in pre_approval_bars:
                if self._entry_touched_on_bar(sig, bar.high, bar.low, bar.close):
                    await self.lifecycle.transition(
                        session,
                        sig,
                        SignalStatus.MISSED_ENTRY.value,
                        "LATE_APPROVAL_ENTRY_TOUCHED",
                        payload={"candle_ms": bar.time_ms},
                    )
                    await session.commit()
                    return sig, "LATE_APPROVAL_ENTRY_TOUCHED"

        sig.telegram_approved = True
        sig.telegram_approved_at = now
        sig.telegram_approved_by = str(user_id)
        await self.lifecycle.log_event(
            session,
            sig,
            "TELEGRAM_APPROVED",
            sig.status,
            sig.status,
            payload={"user_id": user_id, "source": source, "approved_at": now.isoformat()},
        )
        await session.commit()
        await session.refresh(sig)
        return sig, None

    async def reject(
        self,
        session: AsyncSession,
        public_id: str,
        user_id: int,
        source: str = "telegram",
    ) -> tuple[Signal | None, str | None]:
        if not self.user_allowed(user_id, source):
            return None, "UNAUTHORIZED_USER"

        sig = await self._get_signal_for_update(session, public_id)
        if not sig:
            return None, "NOT_FOUND"

        if sig.status in TERMINAL_STATUSES or sig.status == SignalStatus.ACTIVE.value:
            return sig, "SIGNAL_NOT_APPROVABLE"

        if sig.status in (SignalStatus.REJECTED.value, SignalStatus.EXPIRED.value):
            return sig, "ALREADY_CLOSED"

        await self.lifecycle.transition(
            session,
            sig,
            SignalStatus.REJECTED.value,
            "TELEGRAM_REJECTED",
            payload={"user_id": user_id, "source": source},
        )
        await session.commit()
        return sig, None
