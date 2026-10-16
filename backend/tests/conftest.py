import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("SKIP_CONFIG_GUARD", "1")
os.environ.setdefault("DISABLE_APP_BACKGROUND", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/test_tradeonbu.db")
os.environ.setdefault("DASHBOARD_API_KEY", "test-dashboard-key-0123456789")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key-0123456789ab")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "111,222")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "x" * 32)

from app.config import get_settings
from app.db import Base, get_db
from app.main import app

get_settings.cache_clear()


def with_immediate_monitor_fields(structured: dict) -> dict:
    """Fields required for immediate LONG/SHORT persistence (production contract)."""
    return {
        **structured,
        "confirmation_type": structured.get("confirmation_type", "sweep_mss_poi"),
        "structural_invalidation_level": structured.get(
            "structural_invalidation_level", structured.get("stop_loss", 97.0) - 1.0
        ),
    }


from app.db import _sqlite_engine_kwargs

TEST_ENGINE = create_async_engine(
    os.environ["DATABASE_URL"],
    echo=False,
    **_sqlite_engine_kwargs(os.environ["DATABASE_URL"]),
)
TestSession = async_sessionmaker(TEST_ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def delegate_monitor_kline_fetch(monkeypatch, request):
    """Keep legacy tests working: monitor tick uses mocked fetch_kline_bundle."""
    if request.node.get_closest_marker("no_monitor_fetch_delegate"):
        return

    async def _delegate(market, symbol, setups, *, now_ms):
        from app.services.monitor_kline_backfill import aggregate_monitor_kline_limit

        limit, per = aggregate_monitor_kline_limit(setups, now_ms=now_ms, interval="5m")
        cap = min(limit, 200)
        bundle = await market.fetch_kline_bundle(symbol, "5m", cap)
        return bundle, {
            "aggregate_limit": limit,
            "per_setup": per,
            "fetch_mode": "recent_window",
        }

    monkeypatch.setattr(
        "app.services.opportunity_monitor.fetch_monitor_5m_bundle",
        _delegate,
    )


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    from app.services.settings_runtime import reset_settings_overlay

    reset_settings_overlay()
    get_settings.cache_clear()
    async with TEST_ENGINE.begin() as conn:
        from sqlalchemy import text

        if conn.dialect.name == "sqlite":
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA busy_timeout=30000"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    async with TestSession() as s:
        yield s


@pytest_asyncio.fixture
async def client():
    async def _override_db():
        async with TestSession() as s:
            yield s

    app.dependency_overrides[get_db] = _override_db
    transport = ASGITransport(app=app)
    headers = {"X-API-Key": os.environ["DASHBOARD_API_KEY"]}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin_client():
    async def _override_db():
        async with TestSession() as s:
            yield s

    app.dependency_overrides[get_db] = _override_db
    transport = ASGITransport(app=app)
    headers = {"X-API-Key": os.environ["ADMIN_API_KEY"]}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        yield ac
    app.dependency_overrides.clear()
