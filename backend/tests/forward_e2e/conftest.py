"""Isolated SQLite DB for forward paper E2E (never production/staging)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("SKIP_CONFIG_GUARD", "1")
os.environ.setdefault("DISABLE_APP_BACKGROUND", "1")

_FORWARD_DB = Path(__file__).resolve().parents[2] / "data" / "test_forward_paper_e2e.db"
_FORWARD_DB.parent.mkdir(parents=True, exist_ok=True)
FORWARD_DATABASE_URL = f"sqlite+aiosqlite:///{_FORWARD_DB.as_posix()}"

# Do not override global DATABASE_URL — keeps root test suite on test_tradeonbu.db.
os.environ.setdefault("DASHBOARD_API_KEY", "test-dashboard-key-0123456789")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key-0123456789ab")

from app.config import get_settings
from app.db import Base, _sqlite_engine_kwargs
from app.services.settings_runtime import reset_settings_overlay

get_settings.cache_clear()

FORWARD_ENGINE = create_async_engine(
    FORWARD_DATABASE_URL,
    echo=False,
    **_sqlite_engine_kwargs(FORWARD_DATABASE_URL),
)
ForwardSession = async_sessionmaker(FORWARD_ENGINE, expire_on_commit=False)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "forward_e2e: isolated forward paper trading validation (separate test DB file)",
    )


@pytest_asyncio.fixture(autouse=True)
async def prepare_forward_db():
    from tests.db_safety import assert_destructive_db_allowed

    assert_destructive_db_allowed(FORWARD_DATABASE_URL)
    reset_settings_overlay()
    get_settings.cache_clear()
    async with FORWARD_ENGINE.begin() as conn:
        from sqlalchemy import text

        if conn.dialect.name == "sqlite":
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA busy_timeout=30000"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest_asyncio.fixture
async def forward_session() -> AsyncSession:
    async with ForwardSession() as session:
        yield session


@pytest.fixture(autouse=True)
def forbid_live_market_http(monkeypatch):
    """Forward E2E uses ScriptedMarket only — block accidental MarketDataService HTTP."""
    from app.services.market_data import MarketDataService

    async def _forbidden(self, *args, **kwargs):
        raise RuntimeError(
            "Forward E2E must not call live MarketDataService; inject ScriptedMarket on monitors/services"
        )

    for method in (
        "fetch_ticker",
        "fetch_klines",
        "fetch_closed_klines",
        "fetch_historical_closed",
        "fetch_kline_bundle",
        "fetch_live_price",
    ):
        monkeypatch.setattr(MarketDataService, method, _forbidden)


@pytest.fixture(autouse=True)
def delegate_monitor_kline_fetch_forward(monkeypatch, request):
    if request.node.get_closest_marker("no_monitor_fetch_delegate"):
        return
    async def _delegate(market, symbol, setups, *, now_ms):
        from app.services.monitor_kline_backfill import aggregate_monitor_kline_limit

        limit, per = aggregate_monitor_kline_limit(setups, now_ms=now_ms, interval="5m")
        cap = min(limit, 400)
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
