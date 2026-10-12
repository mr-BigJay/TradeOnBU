from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.domain.time_utils import ensure_utc
from app.domain.state_machine import TERMINAL_STATUSES
from app.models import Signal, SignalStatus
from app.services.approval_precheck import ApprovalPrecheckService
from app.services.market_data import MarketDataService
from app.services.signal_lifecycle import SignalLifecycleService


APPROVAL_ALLOWED_STATUSES = frozenset({SignalStatus.WAITING_FOR_ENTRY.value, SignalStatus.DATA_ERROR.value})


class TelegramApprovalService:
    def __init__(self, settings: Settings | None = None, market: MarketDataService | None = None) -> None:
        self.settings = settings or get_settings()
        self.lifecycle = SignalLifecycleService()
        self.precheck = ApprovalPrecheckService(market or MarketDataService(self.settings))

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

    async def approve(
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

        if sig.status in (SignalStatus.MISSED_ENTRY.value, SignalStatus.INVALIDATED.value, SignalStatus.EXPIRED.value):
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

        approval_ms = int(now.timestamp() * 1000)
        check = await self.precheck.evaluate(sig, approval_ms, int(time.time() * 1000))
        if not check.ok:
            if check.reason == "LATE_APPROVAL_ENTRY_TOUCHED":
                await self.lifecycle.transition(
                    session,
                    sig,
                    SignalStatus.MISSED_ENTRY.value,
                    "LATE_APPROVAL_ENTRY_TOUCHED",
                    payload={"candle_ms": check.touched_candle_ms, "policy": check.policy_note},
                )
                await session.commit()
                return sig, check.reason
            return sig, check.reason or "PRECHECK_FAILED"

        sig.telegram_approved = True
        sig.telegram_approved_at = now
        sig.telegram_approved_by = str(user_id)
        sig.entry_monitor_from_ms = check.entry_monitor_from_ms
        sig.analysis = {
            **sig.analysis,
            "approval_policy": check.policy_note,
            "entry_monitor_from_ms": check.entry_monitor_from_ms,
        }
        await self.lifecycle.log_event(
            session,
            sig,
            "TELEGRAM_APPROVED",
            sig.status,
            sig.status,
            payload={
                "user_id": user_id,
                "source": source,
                "approved_at": now.isoformat(),
                "entry_monitor_from_ms": check.entry_monitor_from_ms,
            },
            note=check.policy_note,
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
