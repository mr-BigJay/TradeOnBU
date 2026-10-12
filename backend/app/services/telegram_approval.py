from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Signal, SignalStatus
from app.services.signal_lifecycle import SignalLifecycleService


class TelegramApprovalService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.lifecycle = SignalLifecycleService()

    def user_allowed(self, user_id: int, source: str = "telegram") -> bool:
        if source == "dashboard":
            return True
        allow = self.settings.allowed_telegram_users()
        if not allow:
            return True
        return user_id in allow

    async def approve(
        self,
        session: AsyncSession,
        public_id: str,
        user_id: int,
        source: str = "telegram",
    ) -> tuple[Signal | None, str | None]:
        if not self.user_allowed(user_id, source):
            return None, "UNAUTHORIZED_USER"

        result = await session.execute(select(Signal).where(Signal.public_id == public_id))
        sig = result.scalar_one_or_none()
        if not sig:
            return None, "NOT_FOUND"

        if sig.status in (SignalStatus.EXPIRED.value, SignalStatus.REJECTED.value, SignalStatus.INVALIDATED.value):
            return sig, "SIGNAL_CLOSED"

        if sig.telegram_approved is True:
            return sig, "ALREADY_APPROVED"

        if sig.pending_expires_at and datetime.now(timezone.utc) >= sig.pending_expires_at:
            await self.lifecycle.transition(session, sig, SignalStatus.EXPIRED.value, "PENDING_EXPIRED")
            await session.commit()
            return sig, "SIGNAL_EXPIRED"

        sig.telegram_approved = True
        sig.telegram_approved_at = datetime.now(timezone.utc)
        sig.telegram_approved_by = str(user_id)
        await self.lifecycle.log_event(
            session,
            sig,
            "TELEGRAM_APPROVED",
            sig.status,
            sig.status,
            payload={"user_id": user_id, "source": source},
        )
        await session.commit()
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

        result = await session.execute(select(Signal).where(Signal.public_id == public_id))
        sig = result.scalar_one_or_none()
        if not sig:
            return None, "NOT_FOUND"

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
