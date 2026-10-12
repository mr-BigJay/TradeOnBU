from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import Signal
from app.services.system_state import get_system_state
from app.services.telegram_approval import TelegramApprovalService
from app.services.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)


class TelegramBotService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.notifier = TelegramNotifier(self.settings)
        self.approval = TelegramApprovalService(self.settings)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.telegram_bot_token)

    @property
    def polling_enabled(self) -> bool:
        return self.enabled and not self.settings.effective_telegram_webhook()

    async def _api(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/{method}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()

    async def answer_callback(self, callback_id: str, text: str, show_alert: bool = False) -> None:
        await self._api(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text, "show_alert": show_alert},
        )

    async def edit_message(self, chat_id: int | str, message_id: int, text: str) -> None:
        await self._api(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"},
        )

    def _valid_public_id(self, public_id: str) -> bool:
        try:
            UUID(public_id)
            return bool(UUID_RE.match(public_id))
        except ValueError:
            return False

    def _chat_allowed(self, chat_id: int | str | None) -> bool:
        if chat_id is None:
            return False
        expected = str(self.settings.telegram_chat_id).strip()
        return expected != "" and str(chat_id) == expected

    async def handle_update(self, update: dict[str, Any]) -> None:
        cb = update.get("callback_query")
        if not cb:
            return
        data = cb.get("data") or ""
        user = cb.get("from") or {}
        user_id = int(user.get("id", 0))
        callback_id = cb.get("id")
        message = cb.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")

        if not self._chat_allowed(chat_id):
            if callback_id:
                await self.answer_callback(callback_id, "Unexpected chat")
            return

        parts = data.split(":", 1)
        if len(parts) != 2:
            if callback_id:
                await self.answer_callback(callback_id, "Malformed callback")
            return

        action, public_id = parts[0].strip(), parts[1].strip()
        if action not in {"approve", "reject", "chart"} or not self._valid_public_id(public_id):
            if callback_id:
                await self.answer_callback(callback_id, "Malformed callback")
            return

        async with SessionLocal() as session:
            if action == "approve":
                sig, err = await self.approval.approve(session, public_id, user_id, source="telegram")
                if err == "UNAUTHORIZED_USER":
                    await self.answer_callback(callback_id, "Unauthorized user")
                    return
                if err == "ALREADY_APPROVED":
                    await self.answer_callback(callback_id, "Already approved")
                    return
                if err in ("SIGNAL_EXPIRED", "SIGNAL_NOT_APPROVABLE", "LATE_APPROVAL_ENTRY_TOUCHED"):
                    await self.answer_callback(callback_id, "Signal no longer valid")
                    if chat_id and message_id and sig:
                        await self.edit_message(
                            chat_id,
                            message_id,
                            self.notifier._format_signal(sig, "Signal Closed", f"Reason: {err}\n"),
                        )
                    return
                await self.answer_callback(callback_id, "Paper monitoring enabled")
                if sig and chat_id and message_id:
                    await self.edit_message(
                        chat_id,
                        message_id,
                        self.notifier._format_signal(sig, "Approved — Paper Monitoring", f"Approved by `{user_id}`\n"),
                    )
            elif action == "reject":
                sig, err = await self.approval.reject(session, public_id, user_id, source="telegram")
                if err == "UNAUTHORIZED_USER":
                    await self.answer_callback(callback_id, "Unauthorized user")
                    return
                await self.answer_callback(callback_id, "Signal rejected")
                if sig and chat_id and message_id:
                    await self.edit_message(
                        chat_id,
                        message_id,
                        self.notifier._format_signal(sig, "Rejected", f"Rejected by `{user_id}`\n"),
                    )
            elif action == "chart":
                url = self.notifier.public_chart_url(public_id)
                if url:
                    await self.answer_callback(callback_id, "Opening chart…")
                    await self.notifier.send_message(f"[View Chart]({url})\nSignal `{public_id}`")
                else:
                    await self.answer_callback(
                        callback_id,
                        "Public HTTPS dashboard URL is not configured for this environment.",
                        show_alert=True,
                    )

    async def _persist_offset(self, offset: int) -> None:
        async with SessionLocal() as session:
            state = await get_system_state(session)
            state.telegram_update_offset = offset
            await session.commit()

    async def _load_offset(self) -> int:
        async with SessionLocal() as session:
            state = await get_system_state(session)
            return state.telegram_update_offset or 0

    async def poll_once(self) -> None:
        if not self.polling_enabled:
            return
        offset = await self._load_offset()
        params: dict[str, Any] = {"timeout": 20, "offset": offset}
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/getUpdates"
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.get(url, params=params)
            body = resp.json()
        for upd in body.get("result", []):
            update_id = upd["update_id"]
            try:
                await self.handle_update(upd)
            except Exception:
                logger.exception("Telegram update failed update_id=%s", update_id)
                break
            await self._persist_offset(update_id + 1)
