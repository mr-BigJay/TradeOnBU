from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import Signal
from app.services.telegram_approval import TelegramApprovalService
from app.services.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class TelegramBotService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.notifier = TelegramNotifier(self.settings)
        self.approval = TelegramApprovalService(self.settings)
        self._offset: int | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.telegram_bot_token)

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

        parts = data.split(":", 2)
        if len(parts) != 2:
            if callback_id:
                await self.answer_callback(callback_id, "Unknown action")
            return

        action, public_id = parts
        async with SessionLocal() as session:
            if action == "approve":
                sig, err = await self.approval.approve(session, public_id, user_id)
                if err == "UNAUTHORIZED_USER":
                    await self.answer_callback(callback_id, "Unauthorized user")
                    return
                if err == "ALREADY_APPROVED":
                    await self.answer_callback(callback_id, "Already approved")
                    return
                if err in ("SIGNAL_EXPIRED", "SIGNAL_CLOSED"):
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
                        self.notifier._format_signal(
                            sig,
                            "Approved — Paper Monitoring",
                            f"Approved by `{user_id}`\n",
                        ),
                    )
            elif action == "reject":
                sig, err = await self.approval.reject(session, public_id, user_id)
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
                chart_url = f"{self.settings.dashboard_public_url.rstrip('/')}/?signal={public_id}"
                await self.answer_callback(callback_id, f"Chart: {chart_url}", show_alert=True)
                result = await session.execute(select(Signal).where(Signal.public_id == public_id))
                sig = result.scalar_one_or_none()
                if sig and chat_id:
                    await self.notifier.send_message(f"[View Chart]({chart_url})\nSignal `{public_id}`", parse_mode="Markdown")
            else:
                await self.answer_callback(callback_id, "Unknown action")

    async def poll_once(self) -> None:
        if not self.enabled or self.settings.telegram_use_webhook:
            return
        params: dict[str, Any] = {"timeout": 20}
        if self._offset is not None:
            params["offset"] = self._offset
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/getUpdates"
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.get(url, params=params)
            body = resp.json()
        for upd in body.get("result", []):
            self._offset = upd["update_id"] + 1
            try:
                await self.handle_update(upd)
            except Exception:
                logger.exception("Telegram update failed")
