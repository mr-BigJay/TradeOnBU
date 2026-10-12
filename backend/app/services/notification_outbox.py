from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NotificationOutbox, Signal
from app.services.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5


class NotificationOutboxService:
    def __init__(self, notifier: TelegramNotifier | None = None) -> None:
        self.notifier = notifier or TelegramNotifier()

    async def enqueue(
        self,
        session: AsyncSession,
        dedupe_key: str,
        event_type: str,
        signal: Signal,
        payload: dict | None = None,
    ) -> NotificationOutbox:
        existing = (
            await session.execute(select(NotificationOutbox).where(NotificationOutbox.dedupe_key == dedupe_key))
        ).scalar_one_or_none()
        if existing:
            return existing
        row = NotificationOutbox(
            dedupe_key=dedupe_key,
            event_type=event_type,
            signal_public_id=signal.public_id,
            payload=payload or {},
            status="pending",
        )
        session.add(row)
        await session.flush()
        return row

    async def _send(self, signal: Signal, event_type: str) -> tuple[bool, int | None]:
        if not self.notifier.enabled:
            return False, None
        if event_type == "NEW":
            msg_id = await self.notifier.notify_new_setup(signal)
            return msg_id is not None, msg_id
        handlers = {
            "ACTIVE": lambda: self.notifier.notify_entry_activated(signal),
            "MISSED_ENTRY": lambda: self.notifier.notify_missed_entry(signal),
            "EXPIRED": lambda: self.notifier.notify_expired(signal),
            "TIME_EXIT": lambda: self.notifier.notify_time_exit(signal),
            "TP_HIT": lambda: self.notifier.notify_outcome(signal),
            "SL_HIT": lambda: self.notifier.notify_outcome(signal),
        }
        fn = handlers.get(event_type)
        if not fn:
            return True, None
        msg_id = await fn()
        return msg_id is not None, msg_id

    async def flush_pending(self, session: AsyncSession, limit: int = 50) -> int:
        rows = (
            await session.execute(
                select(NotificationOutbox)
                .where(NotificationOutbox.status == "pending")
                .order_by(NotificationOutbox.id)
                .limit(limit)
            )
        ).scalars().all()
        sent = 0
        for row in rows:
            if row.attempts >= MAX_ATTEMPTS:
                row.status = "failed"
                continue
            sig = (
                await session.execute(select(Signal).where(Signal.public_id == row.signal_public_id))
            ).scalar_one_or_none()
            if not sig:
                row.status = "failed"
                row.last_error = "signal missing"
                continue
            row.attempts += 1
            try:
                ok, msg_id = await self._send(sig, row.event_type)
                if ok:
                    row.status = "sent"
                    row.sent_at = datetime.now(timezone.utc)
                    if msg_id is not None:
                        row.telegram_message_id = msg_id
                    sent += 1
                else:
                    row.last_error = (
                        "telegram disabled" if not self.notifier.enabled else "telegram API returned no message_id"
                    )
            except Exception as exc:
                row.last_error = str(exc)
                logger.exception("Outbox send failed %s", row.dedupe_key)
        await session.commit()
        return sent
