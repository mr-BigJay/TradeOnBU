from __future__ import annotations

import asyncio
import logging

from app.services.telegram_bot import TelegramBotService

logger = logging.getLogger(__name__)


class TelegramPollingCoordinator:
    """Single process-wide Telegram long-polling consumer."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._bot = TelegramBotService()

    async def _loop(self) -> None:
        logger.info("Telegram polling loop started")
        while True:
            try:
                if self._bot.polling_enabled:
                    await self._bot.poll_once()
                else:
                    await asyncio.sleep(2)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegram polling error")
            await asyncio.sleep(0.5)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="telegram_poll")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def notify_settings_changed(self) -> None:
        """Settings reload is lazy via get_effective_settings(); ensure loop is running."""
        self.start()


_coordinator: TelegramPollingCoordinator | None = None


def get_telegram_coordinator() -> TelegramPollingCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = TelegramPollingCoordinator()
    return _coordinator
