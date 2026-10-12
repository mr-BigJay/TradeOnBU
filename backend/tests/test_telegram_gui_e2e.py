"""Telegram GUI integration with mocked HTTP."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.db import SessionLocal, init_db
from app.models import InstallationMeta, User
from app.services.session_auth import create_admin_user
from app.services.settings_runtime import refresh_settings_cache
from app.services.settings_store import set_plain, set_secret


@pytest.mark.asyncio
async def test_gui_telegram_send_test_confirms_delivery(monkeypatch):
    monkeypatch.setenv("SESSION_AUTH_ENABLED", "1")
    monkeypatch.setenv("SKIP_CONFIG_GUARD", "1")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    await init_db()
    async with SessionLocal() as session:
        await create_admin_user(session, "admin", "SecurePass123")
        session.add(InstallationMeta(id=1, installed=True, domain="t.local"))
        await set_secret(session, "telegram", "bot_token", "123456789:TESTTOKEN")
        await set_plain(session, "telegram", "chat_id", "999")
        await set_plain(session, "telegram", "enabled", "true")
        await session.commit()
        await refresh_settings_cache(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        login = await ac.post("/api/auth/login", json={"username": "admin", "password": "SecurePass123"})
        csrf = login.json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}

        async def fake_send(self, text, parse_mode="Markdown", reply_markup=None):
            return 42

        monkeypatch.setattr("app.routes.settings_gui.TelegramNotifier.send_message", fake_send)

        r = await ac.post("/api/gui/settings/telegram/send-test", cookies=login.cookies, headers=headers)
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["message_id"] == 42
