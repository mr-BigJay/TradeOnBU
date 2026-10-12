import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.db import SessionLocal, init_db
from app.models import InstallationMeta, User
from app.services.session_auth import create_admin_user


@pytest_asyncio.fixture
async def session_app(monkeypatch):
    monkeypatch.setenv("SESSION_AUTH_ENABLED", "1")
    monkeypatch.setenv("SKIP_CONFIG_GUARD", "1")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    from tests.conftest import TestSession

    async with TestSession() as session:
        if not (await session.execute(__import__("sqlalchemy").select(User))).scalars().first():
            await create_admin_user(session, "admin", "SecurePass123")
        from sqlalchemy import select

        meta = (await session.execute(select(InstallationMeta).where(InstallationMeta.id == 1))).scalar_one_or_none()
        if meta is None:
            session.add(InstallationMeta(id=1, installed=True, domain="test.local"))
        else:
            meta.installed = True
        await session.commit()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_login_logout(session_app):
    bad = await session_app.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert bad.status_code == 401
    ok = await session_app.post("/api/auth/login", json={"username": "admin", "password": "SecurePass123"})
    assert ok.status_code == 200
    cookie = ok.cookies.get("tradeonbu_session")
    assert cookie
    csrf = ok.json()["csrf_token"]
    me = await session_app.get("/api/auth/me", cookies=ok.cookies)
    assert me.json()["authenticated"] is True
    protected = await session_app.get("/api/signals", cookies=ok.cookies)
    assert protected.status_code == 200
    out = await session_app.post("/api/auth/logout", cookies=ok.cookies)
    assert out.status_code == 200


@pytest.mark.asyncio
async def test_settings_require_auth(monkeypatch):
    monkeypatch.setenv("SESSION_AUTH_ENABLED", "1")
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/api/gui/settings/telegram")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_telegram_settings_no_secret_leak(session_app):
    login = await session_app.post("/api/auth/login", json={"username": "admin", "password": "SecurePass123"})
    csrf = login.json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}
    await session_app.put(
        "/api/gui/settings/telegram",
        json={"enabled": True, "bot_token": "123456789:ABCDEF", "chat_id": "999", "allowed_user_ids": "111"},
        cookies=login.cookies,
        headers=headers,
    )
    view = await session_app.get("/api/gui/settings/telegram", cookies=login.cookies)
    body = view.json()
    assert "ABCDEF" not in str(body)
    assert body.get("bot_token_masked")


def test_install_domain_validation():
    import subprocess

    root = __import__("pathlib").Path(__file__).resolve().parents[2]
    ok = subprocess.run(["bash", "-c", f'source "{root}/scripts/install_lib.sh" && validate_domain trade.example.com'], check=False).returncode
    assert ok == 0
