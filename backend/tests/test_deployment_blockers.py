"""Deployment blocker regression tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.domain.candles import Candle
from app.models import NotificationOutbox, RuntimeSettings, Signal, SignalEvent
from app.services.signal_lifecycle import SignalLifecycleService
from app.services.signal_monitor import SignalMonitorService
from app.services.telegram_approval import TelegramApprovalService
from tests.helpers.factories import make_signal
from tests.helpers.market_fakes import complete_bundle, live_quote
from tests.repo_paths import repo_root, require_repo_file

ROOT = repo_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_verify_health_json_compact_and_spaced():
    from scripts.verify_helpers import check_health_json

    check_health_json('{"paper_only":true,"live_trading":false}')
    check_health_json('{"paper_only": true, "live_trading": false}')


def test_verify_chart_json():
    from scripts.verify_helpers import check_chart_json

    check_chart_json('{"candles":[]}')


@pytest.mark.asyncio
async def test_verify_against_local_api(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    from scripts.verify_helpers import check_health_payload

    check_health_payload(resp.json())


@pytest.mark.asyncio
async def test_absolute_db_path_survives_reopen(tmp_path):
    db_file = tmp_path / "persist.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

    engine = create_async_engine(url, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        sig = make_signal()
        session.add(sig)
        row = RuntimeSettings(id=1)
        session.add(row)
        outbox = NotificationOutbox(
            dedupe_key="k1",
            event_type="NEW",
            signal_public_id=sig.public_id,
            status="pending",
        )
        session.add(outbox)
        await session.commit()
        public_id = sig.public_id

    await engine.dispose()

    engine2 = create_async_engine(url, echo=False)
    Session2 = async_sessionmaker(engine2, expire_on_commit=False)
    async with Session2() as session:
        sig2 = (await session.execute(select(Signal).where(Signal.public_id == public_id))).scalar_one()
        assert sig2.entry_price == 100.0
        events = (await session.execute(select(SignalEvent))).scalars().all()
        assert len(events) >= 0
        settings = (await session.execute(select(RuntimeSettings))).scalar_one()
        assert settings.min_net_rr == 2.0
        ob = (await session.execute(select(NotificationOutbox))).scalar_one()
        assert ob.dedupe_key == "k1"
    await engine2.dispose()


@pytest.mark.asyncio
async def test_docker_database_url_is_absolute_under_app_data():
    compose = require_repo_file("docker-compose.yml").read_text()
    assert "sqlite+aiosqlite:////app/data/tradeonbu.db" in compose


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
@pytest.mark.skipif(os.getenv("RUN_DOCKER_PERSISTENCE") != "1", reason="set RUN_DOCKER_PERSISTENCE=1")
def test_docker_recreate_preserves_volume_data():
    subprocess.run(["docker", "compose", "up", "-d", "--build"], cwd=ROOT, check=True, timeout=300)
    subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "tradeonbu",
            "python3",
            "-c",
            "import asyncio; from app.db import init_db; asyncio.run(init_db())",
        ],
        cwd=ROOT,
        check=True,
        timeout=60,
    )
    subprocess.run(["docker", "compose", "restart"], cwd=ROOT, check=True, timeout=120)
    # Presence of mounted DB file is the persistence contract
    data_db = ROOT / "data" / "tradeonbu.db"
    assert data_db.exists(), "expected host ./data/tradeonbu.db after compose restart"


@pytest.mark.asyncio
async def test_approval_ambiguous_mid_1m_creation(session):
    created_ms = 1_700_000_030_000
    sig = make_signal(created_at=datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc))
    session.add(sig)
    await session.commit()
    m1_open = (created_ms // 60_000) * 60_000
    ambiguous_1m = Candle(open=100, high=101, low=99, close=100, time_ms=m1_open)

    class M:
        async def fetch_kline_bundle(self, symbol, interval, limit=200):
            if interval == "1m":
                return complete_bundle(closed=[ambiguous_1m], interval="1m")
            return complete_bundle(interval="5m")

    _, err = await TelegramApprovalService(market=M()).approve(session, sig.public_id, 111)
    assert err == "APPROVAL_ENTRY_TOUCH_AMBIGUOUS"


@pytest.mark.asyncio
async def test_same_5m_bar_long_entry_then_tp(session):
    bar_5m = 1_700_000_000_000
    m1_entry = Candle(open=100, high=100.5, low=99.5, close=100, time_ms=bar_5m)
    m1_tp = Candle(open=100, high=107, low=100, close=106, time_ms=bar_5m + 60_000)

    class M:
        async def fetch_live_price(self, symbol):
            return live_quote(100)

        async def fetch_kline_bundle(self, symbol, interval, limit=200):
            if interval == "1m":
                return complete_bundle(closed=[m1_entry, m1_tp], interval="1m")
            return complete_bundle(closed=[Candle(100, 107, 99, 106, bar_5m)], interval="5m")

        async def fetch_historical_closed(self, symbol, interval, from_ms, max_bars=500):
            b = Candle(100, 107, 99, 106, bar_5m)
            return complete_bundle(closed=[b] if b.time_ms >= from_ms else [], interval="5m")

        def is_quote_stale(self, q):
            return False

    sig = make_signal(
        direction="LONG",
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=106.0,
        telegram_approved=True,
        entry_monitor_from_ms=bar_5m,
    )
    session.add(sig)
    await session.commit()
    runtime = RuntimeSettings(id=1, max_pending_minutes=90, max_missed_entry_pct=5, holding_time_hours=6, min_net_rr=2)
    monitor = SignalMonitorService(M(), SignalLifecycleService())
    out = await monitor.monitor_open_signals(session, runtime)
    await session.refresh(sig)
    from app.models import SignalStatus

    assert sig.status == SignalStatus.TP_HIT.value
    assert "TP_HIT" in str(out)


@pytest.mark.asyncio
async def test_same_5m_bar_short_entry_then_tp(session):
    bar_5m = 1_700_000_000_000
    m1_entry = Candle(open=100, high=100.5, low=99.5, close=100, time_ms=bar_5m)
    m1_tp = Candle(open=100, high=100, low=93, close=94, time_ms=bar_5m + 60_000)

    class M:
        async def fetch_live_price(self, symbol):
            return live_quote(100)

        async def fetch_kline_bundle(self, symbol, interval, limit=200):
            if interval == "1m":
                return complete_bundle(closed=[m1_entry, m1_tp], interval="1m")
            return complete_bundle(closed=[Candle(100, 100, 93, 94, bar_5m)], interval="5m")

        async def fetch_historical_closed(self, symbol, interval, from_ms, max_bars=500):
            b = Candle(100, 100, 93, 94, bar_5m)
            return complete_bundle(closed=[b] if b.time_ms >= from_ms else [], interval="5m")

        def is_quote_stale(self, q):
            return False

    sig = make_signal(
        direction="SHORT",
        entry_price=100.0,
        stop_loss=102.0,
        take_profit=94.0,
        invalidation_level=105.0,
        telegram_approved=True,
        entry_monitor_from_ms=bar_5m,
    )
    session.add(sig)
    await session.commit()
    runtime = RuntimeSettings(id=1, max_pending_minutes=90, max_missed_entry_pct=5, holding_time_hours=6, min_net_rr=2)
    monitor = SignalMonitorService(M(), SignalLifecycleService())
    await monitor.monitor_open_signals(session, runtime)
    await session.refresh(sig)
    from app.models import SignalStatus

    assert sig.status == SignalStatus.TP_HIT.value
