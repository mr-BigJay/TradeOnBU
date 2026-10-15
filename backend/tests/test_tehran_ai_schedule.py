"""Tehran :30 hourly fixed schedule for scheduled ICT analysis."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.domain.tehran_time import TEHRAN
from app.models import AiAnalysisSettings, ChatGptCredential
from app.services.ai_analysis_executor import execute_scheduled_if_due
from app.services.ai_analysis_scheduler import (
    claim_scheduled_slot,
    due_scheduled_slot,
    record_run_outcome,
    should_run_scheduled,
)
from app.services.ai_tehran_schedule import (
    SCHEDULE_OFFSET_SECONDS,
    active_due_slot_fire_utc,
    next_tehran_scheduled_fire_utc,
    slot_fire_ms,
)


def _utc(y, m, d, h, mi, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=TEHRAN).astimezone(timezone.utc)


def test_next_fire_is_upcoming_tehran_30_plus_offset():
    now = _utc(2026, 10, 15, 14, 25, 0)
    nxt = next_tehran_scheduled_fire_utc(now)
    local = nxt.astimezone(TEHRAN)
    assert local.hour == 14 and local.minute == 30 and local.second == SCHEDULE_OFFSET_SECONDS


def test_active_due_slot_within_hour_window():
    now = _utc(2026, 10, 15, 3, 45, 0)
    due = active_due_slot_fire_utc(now)
    assert due is not None
    loc = due.astimezone(TEHRAN)
    assert loc.hour == 3 and loc.minute == 30 and loc.second == SCHEDULE_OFFSET_SECONDS


def test_missed_previous_slot_not_due_after_window():
    """After 04:30:10 window ends, 03:30 slot must not run (no catch-up)."""
    now = _utc(2026, 10, 15, 4, 31, 0)
    cfg = AiAnalysisSettings(last_scheduled_slot_ms=0)
    assert due_scheduled_slot(cfg, now) is not None
    loc = due_scheduled_slot(cfg, now).astimezone(TEHRAN)
    assert loc.hour == 4


def test_current_hour_slot_not_due_before_offset():
    now = _utc(2026, 10, 15, 14, 30, 5)
    due = active_due_slot_fire_utc(now)
    assert due is not None
    assert due.astimezone(TEHRAN).hour == 13


def test_current_hour_slot_due_after_offset():
    now = _utc(2026, 10, 15, 14, 30, 15)
    cfg = AiAnalysisSettings()
    due = due_scheduled_slot(cfg, now)
    assert due is not None
    assert due.astimezone(TEHRAN).hour == 14


def test_one_scheduled_report_per_slot():
    now = _utc(2026, 10, 15, 5, 40, 0)
    due = active_due_slot_fire_utc(now)
    cfg = AiAnalysisSettings()
    claim_scheduled_slot(cfg, due)
    assert due_scheduled_slot(cfg, now) is None


def test_failure_does_not_shift_next_run_to_interval():
    cfg = AiAnalysisSettings(interval_minutes=60)
    before = next_tehran_scheduled_fire_utc()
    record_run_outcome(cfg, "failed", "err", trigger="scheduled")
    after = cfg.next_run_at
    assert after is not None
    assert abs((after - before).total_seconds()) < 5


def test_manual_run_does_not_update_next_run_at():
    cfg = AiAnalysisSettings(next_run_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
    record_run_outcome(cfg, "completed", trigger="manual")
    assert cfg.next_run_at.year == 2030


@pytest.mark.asyncio
async def test_restart_does_not_burst_missed_slots(session):
    session.add(
        AiAnalysisSettings(
            id=1,
            enabled=True,
            unattended_consent=True,
            last_scheduled_slot_ms=0,
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
    frozen = _utc(2026, 10, 15, 8, 5, 0)
    with patch("app.services.ai_analysis_scheduler._utc_now", return_value=frozen):
        with patch("app.services.ai_tehran_schedule.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: (
                frozen.astimezone(tz or timezone.utc)
                if tz
                else frozen.replace(tzinfo=None)
            )
            assert await should_run_scheduled(session) is True
    with patch("app.services.ai_analysis_executor.scheduled_analysis_market_ready", AsyncMock(return_value=(False, "forming_1h_candle"))):
        with patch("app.services.ai_analysis_executor.run_ai_analysis", AsyncMock()) as mock_run:
            out = await execute_scheduled_if_due()
    assert out.status == "not_ready"
    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_lock_prevents_duplicate_scheduled_run(session):
    session.add(
        AiAnalysisSettings(
            id=1,
            enabled=True,
            unattended_consent=True,
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
    with patch("app.services.ai_analysis_executor.scheduled_analysis_market_ready", AsyncMock(return_value=(True, None))):
        with patch("app.services.ai_analysis_executor.acquire_analysis_lock", AsyncMock(return_value=False)):
            with patch("app.services.ai_analysis_executor.run_ai_analysis", AsyncMock()) as mock_run:
                out = await execute_scheduled_if_due()
    assert out.status == "busy"
    mock_run.assert_not_called()


def test_hour_boundary_tehran():
    """02:29 still in 01:30 slot window; 02:30:15 opens 02:30 slot."""
    cfg = AiAnalysisSettings()
    t1 = datetime(2026, 10, 15, 2, 29, 0, tzinfo=ZoneInfo("Asia/Tehran")).astimezone(timezone.utc)
    due1 = due_scheduled_slot(cfg, t1)
    assert due1 is not None
    assert due1.astimezone(TEHRAN).hour == 1
    t2 = datetime(2026, 10, 15, 2, 30, 15, tzinfo=ZoneInfo("Asia/Tehran")).astimezone(timezone.utc)
    due2 = due_scheduled_slot(cfg, t2)
    assert due2 is not None
    assert due2.astimezone(TEHRAN).hour == 2
