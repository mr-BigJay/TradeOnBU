from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.db import SessionLocal, init_db
from app.models import NotificationOutbox
from app.services.notification_outbox import NotificationOutboxService
from app.services.settings_runtime import refresh_settings_cache
from app.services.settings_store import set_plain, set_secret
from app.services.telegram_bot import TelegramBotService
from app.services.telegram_notifier import TelegramNotifier
from app.services.telegram_runtime import telegram_integration_active
from app.worker import BotWorker
from sqlalchemy import select


@pytest.mark.asyncio
async def test_notifier_respects_gui_disabled():
    await init_db()
    async with SessionLocal() as session:
        await set_secret(session, "telegram", "bot_token", "123:ABC")
        await set_plain(session, "telegram", "chat_id", "1")
        await set_plain(session, "telegram", "enabled", "false")
        await session.commit()
        await refresh_settings_cache(session)
    assert telegram_integration_active() is False
    n = TelegramNotifier()
    assert n.enabled is False
    msg_id = await n.send_message("should not send")
    assert msg_id is None


@pytest.mark.asyncio
async def test_outbox_keeps_pending_when_telegram_disabled():
    await init_db()
    async with SessionLocal() as session:
        await set_secret(session, "telegram", "bot_token", "123:ABC")
        await set_plain(session, "telegram", "chat_id", "1")
        await set_plain(session, "telegram", "enabled", "false")
        await session.commit()
        await refresh_settings_cache(session)

    from tests.helpers.factories import make_signal

    async with SessionLocal() as session:
        sig = make_signal(public_id="00000000-0000-4000-8000-000000000099", dedupe_key="test-outbox")
        session.add(sig)
        await session.flush()
        outbox = NotificationOutboxService()
        await outbox.enqueue(session, "NEW:test", "NEW", sig)
        await session.commit()

    async with SessionLocal() as session:
        outbox = NotificationOutboxService()
        sent = await outbox.flush_pending(session)
        assert sent == 0
        row = (await session.execute(select(NotificationOutbox).where(NotificationOutbox.dedupe_key == "NEW:test"))).scalar_one()
        assert row.status == "pending"
        assert "disabled" in (row.last_error or "").lower()


@pytest.mark.asyncio
async def test_telegram_approve_ambiguous_not_success():
    from app.services.telegram_approval import TelegramApprovalService

    bot = TelegramBotService()
    bot.answer_callback = AsyncMock()
    bot.edit_message = AsyncMock()

    with patch("app.services.telegram_bot.telegram_integration_active", return_value=True), patch.object(
        TelegramBotService, "_chat_allowed", return_value=True
    ), patch.object(
        TelegramApprovalService,
        "approve",
        AsyncMock(return_value=(None, "APPROVAL_ENTRY_TOUCH_AMBIGUOUS")),
    ):
        await bot.handle_update(
            {
                "callback_query": {
                    "id": "cb1",
                    "data": "approve:00000000-0000-4000-8000-000000000001",
                    "from": {"id": 42},
                    "message": {"chat": {"id": 1}, "message_id": 9},
                }
            }
        )
    bot.answer_callback.assert_awaited()
    args = bot.answer_callback.await_args
    assert args[0][1] != "Paper monitoring enabled"
    bot.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_dashboard_approval_returns_409_on_precheck_fail(monkeypatch):
    monkeypatch.setenv("SKIP_CONFIG_GUARD", "1")
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        with patch(
            "app.main.TelegramApprovalService.approve",
            AsyncMock(return_value=(None, "INCOMPLETE_MARKET_DATA")),
        ):
            r = await ac.post(
                "/api/signals/telegram-approval",
                json={"public_id": "00000000-0000-4000-8000-000000000001", "approved": True},
                headers={"X-API-Key": "test-admin-key-0123456789ab"},
            )
            assert r.status_code == 409
            assert r.json()["detail"] == "INCOMPLETE_MARKET_DATA"


@pytest.mark.asyncio
async def test_worker_refreshes_execution_model_each_cycle(monkeypatch):
    monkeypatch.setenv("SKIP_CONFIG_GUARD", "1")
    from app.models import RuntimeSettings
    from app.services.settings_runtime import refresh_settings_cache

    async with SessionLocal() as session:
        row = (await session.execute(select(RuntimeSettings).where(RuntimeSettings.id == 1))).scalar_one_or_none()
        if row is None:
            session.add(RuntimeSettings(id=1))
            await session.flush()
        row = (await session.execute(select(RuntimeSettings).where(RuntimeSettings.id == 1))).scalar_one()
        row.margin_usdt = 75.0
        row.paper_trading_paused = True
        await session.commit()
        await refresh_settings_cache(session)

    w = BotWorker()
    await w.run_cycle()
    assert w.lifecycle.paper.settings.margin_usdt == 75.0
    assert w.entry.paper.settings.margin_usdt == 75.0
