from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings, get_effective_settings
from app.services.telegram_runtime import telegram_integration_active
from app.models import Signal

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings_override = settings

    @property
    def settings(self) -> Settings:
        return self._settings_override or get_effective_settings()

    @property
    def enabled(self) -> bool:
        return telegram_integration_active(self.settings)

    def public_chart_url(self, public_id: str) -> str | None:
        url = (self.settings.dashboard_public_url or "").strip()
        if not url.startswith("https://"):
            return None
        if "localhost" in url or "127.0.0.1" in url:
            return None
        return f"{url.rstrip('/')}/?signal={public_id}"

    def _inline_keyboard(self, public_id: str) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve Paper Signal", "callback_data": f"approve:{public_id}"},
                    {"text": "❌ Reject Signal", "callback_data": f"reject:{public_id}"},
                ],
                [{"text": "📊 View Chart", "callback_data": f"chart:{public_id}"}],
            ]
        }

    def _format_signal(self, signal: Signal, title: str, extra: str = "") -> str:
        pnl = signal.estimated_net_pnl_usdt
        pnl_line = f"Estimated Net P&L: {pnl} USDT\n" if pnl is not None else ""
        return (
            f"*{title}*\n"
            f"ID: `{signal.public_id}`\n"
            f"Symbol: {signal.symbol} | {signal.direction}\n"
            f"Status: {signal.status}\n"
            f"Entry: {signal.entry_price} ({signal.entry_type})\n"
            f"SL: {signal.stop_loss} | TP: {signal.take_profit}\n"
            f"Margin: {self.settings.margin_usdt} USDT | Leverage: {self.settings.leverage}x\n"
            f"Net R:R: {signal.net_rr}\n"
            f"{pnl_line}"
            f"{extra}"
            f"\n_Paper simulation only — no Bitunix order placed._"
        )

    async def send_message(
        self,
        text: str,
        parse_mode: str | None = "Markdown",
        reply_markup: dict[str, Any] | None = None,
    ) -> int | None:
        if not self.enabled:
            logger.info("Telegram disabled; message skipped: %s", text[:120])
            return None
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": self.settings.telegram_chat_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.error("Telegram error %s: %s", resp.status_code, resp.text)
                return None
            data = resp.json()
            if not data.get("ok"):
                logger.error("Telegram API not ok: %s", data)
                return None
            return data.get("result", {}).get("message_id")

    async def send_ai_report(self, text: str) -> int | None:
        """Plain text — AI Persian reports may contain Markdown-special characters."""
        return await self.send_message(text, parse_mode=None)

    async def notify_new_setup(self, signal: Signal) -> int | None:
        msg_id = await self.send_message(
            self._format_signal(signal, "New ICT Setup Detected", "Use buttons below to approve paper monitoring.\n"),
            reply_markup=self._inline_keyboard(signal.public_id),
        )
        if msg_id is not None:
            signal.telegram_message_id = msg_id
        return msg_id

    async def notify_entry_activated(self, signal: Signal) -> int | None:
        return await self.send_message(
            self._format_signal(signal, "Simulated Entry Activated", f"Fill: {signal.fill_price}\n")
        )

    async def notify_outcome(self, signal: Signal) -> int | None:
        return await self.send_message(self._format_signal(signal, f"Trade Outcome: {signal.status}"))

    async def notify_missed_entry(self, signal: Signal) -> int | None:
        return await self.send_message(self._format_signal(signal, "Setup Cancelled: Missed Entry"))

    async def notify_expired(self, signal: Signal) -> int | None:
        return await self.send_message(self._format_signal(signal, "Setup Expired"))

    async def notify_time_exit(self, signal: Signal) -> int | None:
        return await self.send_message(self._format_signal(signal, "Time Exit"))

    async def notify_bias_change(self, symbol: str, old_bias: str, new_bias: str) -> int | None:
        return await self.send_message(f"*Market Bias Change*\n{symbol}: {old_bias} → {new_bias}\n_Paper mode only._")
