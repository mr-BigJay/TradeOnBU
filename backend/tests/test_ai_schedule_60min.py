"""AI analysis scheduler: 60-minute default and legacy migration restore."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.routes.chatgpt_ai import SettingsBody
from app.models import AiAnalysisSettings, ChatGptCredential
from app.services.ai_analysis_scheduler import (
    DEFAULT_AI_INTERVAL_MINUTES,
    MAX_AI_INTERVAL_MINUTES,
    MIN_AI_INTERVAL_MINUTES,
    _ensure_utc,
    _reschedule_next_run_after_interval_change,
    get_or_create_settings,
    migrate_ai_schedule_settings,
    record_run_outcome,
)


def test_default_interval_constant_is_sixty():
    assert DEFAULT_AI_INTERVAL_MINUTES == 60
    assert MIN_AI_INTERVAL_MINUTES == 60
    assert MAX_AI_INTERVAL_MINUTES == 360


def test_sixty_minute_interval_not_bumped_on_load():
    cfg = AiAnalysisSettings(
        enabled=True,
        unattended_consent=True,
        interval_minutes=60,
        interval_migrated_from_60=False,
    )
    assert migrate_ai_schedule_settings(cfg) is False
    assert cfg.interval_minutes == 60


def test_legacy_migration_restores_sixty_from_auto_one_twenty():
    nr = datetime.now(timezone.utc) + timedelta(minutes=90)
    last_ok = datetime.now(timezone.utc) - timedelta(minutes=30)
    cfg = AiAnalysisSettings(
        enabled=True,
        unattended_consent=True,
        interval_minutes=120,
        interval_migrated_from_60=True,
        last_success_at=last_ok,
        next_run_at=nr,
    )
    assert migrate_ai_schedule_settings(cfg) is True
    assert cfg.interval_minutes == 60
    assert cfg.interval_migrated_from_60 is False
    assert cfg.next_run_at is not None
    from app.services.ai_tehran_schedule import next_tehran_scheduled_fire_utc

    assert abs((_ensure_utc(cfg.next_run_at) - next_tehran_scheduled_fire_utc()).total_seconds()) < 2


def test_reschedule_syncs_tehran_next_run():
    from app.services.ai_tehran_schedule import next_tehran_scheduled_fire_utc

    cfg = AiAnalysisSettings(interval_minutes=60)
    _reschedule_next_run_after_interval_change(cfg)
    assert abs((cfg.next_run_at - next_tehran_scheduled_fire_utc()).total_seconds()) < 2


def test_manual_one_twenty_not_restored_without_migration_flag():
    cfg = AiAnalysisSettings(
        interval_minutes=120,
        interval_migrated_from_60=False,
    )
    assert migrate_ai_schedule_settings(cfg) is False
    assert cfg.interval_minutes == 120


def test_record_run_outcome_schedules_next_tehran_slot_after_success():
    from app.services.ai_tehran_schedule import next_tehran_scheduled_fire_utc

    cfg = AiAnalysisSettings(interval_minutes=60)
    record_run_outcome(cfg, "completed", trigger="scheduled")
    expected = next_tehran_scheduled_fire_utc()
    assert cfg.next_run_at is not None
    assert abs((cfg.next_run_at - expected).total_seconds()) < 2


@pytest.mark.asyncio
async def test_get_or_create_settings_default_interval(session):
    cfg = await get_or_create_settings(session)
    assert cfg.interval_minutes == DEFAULT_AI_INTERVAL_MINUTES


def test_api_settings_body_accepts_sixty_minutes():
    body = SettingsBody(enabled=False, unattended_consent=False, interval_minutes=60)
    assert body.interval_minutes == 60


def test_api_settings_body_rejects_interval_below_sixty():
    with pytest.raises(ValidationError):
        SettingsBody(enabled=False, unattended_consent=False, interval_minutes=45)


@pytest.mark.asyncio
async def test_api_put_settings_persists_sixty_minutes(client, session):
    r = await client.put(
        "/api/gui/chatgpt/settings",
        json={"enabled": False, "unattended_consent": False, "interval_minutes": 60},
    )
    assert r.status_code == 200
    cfg = await get_or_create_settings(session)
    assert cfg.interval_minutes == 60


def test_opportunity_monitor_tick_independent_of_ai_scheduler():
    import inspect

    from app import main
    from app.config import get_settings

    src = inspect.getsource(main.lifespan)
    assert 'id="ai_ict_tehran_cron"' in src
    assert "minutes=1" in src
    assert 'id="ict_opportunity_monitor"' in src
    assert "opportunity_monitor_interval_seconds" in src
    assert get_settings().opportunity_monitor_interval_seconds == 60


@pytest.mark.asyncio
async def test_scheduled_executor_skips_when_lock_held(session):
    from app.services.ai_analysis_executor import execute_scheduled_if_due

    session.add(
        AiAnalysisSettings(
            id=1,
            enabled=True,
            unattended_consent=True,
            interval_minutes=60,
            next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
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

    with patch("app.services.ai_analysis_executor.acquire_analysis_lock", AsyncMock(return_value=False)):
        with patch("app.services.ai_analysis_executor.run_ai_analysis", AsyncMock()) as mock_run:
            await execute_scheduled_if_due()
    mock_run.assert_not_called()
