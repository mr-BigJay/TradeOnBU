"""Integration: legacy schedule migration persistence and next_run_at safety."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import AiAnalysisSettings, ChatGptCredential
from app.services.ai_analysis_executor import execute_scheduled_if_due
from app.services.ai_analysis_scheduler import (
    _ensure_utc,
    get_or_create_settings,
    should_run_scheduled,
)


def _legacy_row(**overrides) -> AiAnalysisSettings:
    now = datetime.now(timezone.utc)
    base = dict(
        id=1,
        enabled=True,
        unattended_consent=True,
        interval_minutes=120,
        interval_migrated_from_60=True,
        last_success_at=now - timedelta(minutes=30),
        next_run_at=now + timedelta(minutes=90),
    )
    base.update(overrides)
    return AiAnalysisSettings(**base)


@pytest.mark.asyncio
async def test_migration_persisted_after_session_close(session):
    session.add(_legacy_row())
    await session.commit()

    async with SessionLocal() as s1:
        await get_or_create_settings(s1, autocommit_migration=True)

    async with SessionLocal() as s2:
        row = (
            await s2.execute(select(AiAnalysisSettings).where(AiAnalysisSettings.id == 1))
        ).scalar_one()
        assert row.interval_minutes == 60
        assert row.interval_migrated_from_60 is False
        assert row.next_run_at is not None
        floor = _ensure_utc(row.last_success_at) + timedelta(minutes=60)
        assert _ensure_utc(row.next_run_at) >= floor


@pytest.mark.asyncio
async def test_status_get_commits_migration(client, session):
    session.add(_legacy_row())
    await session.commit()

    resp = await client.get("/api/gui/chatgpt/status")
    assert resp.status_code == 200
    assert resp.json()["interval_minutes"] == 60

    async with SessionLocal() as s2:
        row = (
            await s2.execute(select(AiAnalysisSettings).where(AiAnalysisSettings.id == 1))
        ).scalar_one()
        assert row.interval_minutes == 60
        assert row.interval_migrated_from_60 is False


@pytest.mark.asyncio
async def test_migration_next_run_not_before_last_success_plus_sixty(session):
    last_ok = datetime.now(timezone.utc) - timedelta(minutes=10)
    session.add(
        _legacy_row(
            last_success_at=last_ok,
            next_run_at=datetime.now(timezone.utc) + timedelta(minutes=100),
        )
    )
    await session.commit()

    async with SessionLocal() as s1:
        cfg = await get_or_create_settings(s1, autocommit_migration=True)
        assert cfg.interval_minutes == 60
        assert _ensure_utc(cfg.next_run_at) >= last_ok + timedelta(minutes=60)
        assert _ensure_utc(cfg.next_run_at) <= datetime.now(timezone.utc) + timedelta(minutes=55)


@pytest.mark.asyncio
async def test_scheduler_skips_duplicate_run_after_migration(session):
    session.add(
        _legacy_row(
            next_run_at=datetime.now(timezone.utc) + timedelta(minutes=45),
        )
    )
    session.add(
        ChatGptCredential(
            id=1,
            client_id="oai",
            access_token_enc="enc",
            scopes_json=["chatgpt.tokens.use.direct"],
        )
    )
    await session.commit()

    async with SessionLocal() as s1:
        assert await should_run_scheduled(s1) is False

    with patch("app.services.ai_analysis_executor.run_ai_analysis", AsyncMock()) as mock_run:
        outcome = await execute_scheduled_if_due()
    assert outcome.status == "skipped"
    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_put_keeps_one_twenty_when_user_confirms_legacy_flag(client, session):
    session.add(_legacy_row())
    await session.commit()

    r = await client.put(
        "/api/gui/chatgpt/settings",
        json={"enabled": False, "unattended_consent": False, "interval_minutes": 120},
    )
    assert r.status_code == 200

    async with SessionLocal() as s2:
        row = (
            await s2.execute(select(AiAnalysisSettings).where(AiAnalysisSettings.id == 1))
        ).scalar_one()
        assert row.interval_minutes == 120
        assert row.interval_migrated_from_60 is False

    async with SessionLocal() as s3:
        cfg = await get_or_create_settings(s3)
        assert cfg.interval_minutes == 120
