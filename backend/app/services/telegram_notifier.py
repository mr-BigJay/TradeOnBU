from __future__ import annotations

import asyncio
import logging
from pathlib import Path
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

    @staticmethod
    def _retry_after_seconds(resp: httpx.Response) -> float:
        raw = resp.headers.get("Retry-After", "0")
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 0.0

    async def _post_with_retries(self, url: str, **kwargs: Any) -> httpx.Response | None:
        last_status = 0
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(url, **kwargs)
            except httpx.HTTPError as exc:
                logger.warning("Telegram HTTP error (attempt %s): %s", attempt + 1, exc)
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
            except Exception as exc:
                logger.warning("Telegram unexpected error (attempt %s): %s", attempt + 1, exc)
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
            last_status = resp.status_code
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = self._retry_after_seconds(resp)
                await asyncio.sleep(max(retry_after, 0.8 * (attempt + 1)))
                continue
            return resp
        logger.error("Telegram request failed after retries (last status %s)", last_status)
        return None

    @staticmethod
    def _message_id_from_response(resp: httpx.Response) -> int | None:
        try:
            data = resp.json()
        except Exception:
            logger.error("Telegram response JSON parse failed")
            return None
        if not isinstance(data, dict) or not data.get("ok"):
            logger.error("Telegram API response not ok")
            return None
        result = data.get("result") or {}
        if not isinstance(result, dict):
            return None
        msg_id = result.get("message_id")
        return int(msg_id) if msg_id is not None else None

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
            "text": text[:4096],
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            resp = await self._post_with_retries(url, json=payload)
            if resp is None or resp.status_code >= 400:
                if resp is not None:
                    logger.error("Telegram sendMessage error %s", resp.status_code)
                return None
            return self._message_id_from_response(resp)
        except Exception:
            logger.exception("Telegram sendMessage failed")
            return None

    async def send_photo_file(self, photo_path: Path, caption: str) -> int | None:
        if not self.enabled:
            logger.info("Telegram disabled; photo skipped")
            return None
        if not photo_path.is_file():
            logger.error("Telegram photo missing: %s", photo_path.name)
            return None
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendPhoto"
        try:
            data = {"chat_id": self.settings.telegram_chat_id, "caption": caption[:1024]}
            content = photo_path.read_bytes()
            files = {"photo": ("chart.png", content, "image/png")}
            resp = await self._post_with_retries(url, data=data, files=files)
            if resp is None or resp.status_code >= 400:
                if resp is not None:
                    logger.error("Telegram sendPhoto error %s", resp.status_code)
                return None
            return self._message_id_from_response(resp)
        except Exception:
            logger.exception("Telegram sendPhoto failed")
            return None

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
