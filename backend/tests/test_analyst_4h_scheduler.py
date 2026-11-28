"""UTC closed-4H analyst scheduling and persisted paid-attempt admission.

Tests use synthetic clocks; no network or AI calls.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models import AiAnalysisRun, AiAnalysisSettings
from app.services import ai_analysis_scheduler as scheduler


def test_four_hour_boundaries_and_close_grace(monkeypatch):
    monkeypatch.setenv("MULTI_SETUP_ANALYST_ENABLED", "1")
    cfg = AiAnalysisSettings(interval_minutes=60)
    assert scheduler._effective_interval_minutes(cfg) == 240
    for hour, minute, expected in [
        (7, 59, (8, 5)),
        (8, 0, (8, 5)),
        (8, 4, (8, 5)),
        (8, 5, (12, 5)),
        (9, 30, (12, 5)),
        (11, 59, (12, 5)),
        (12, 0, (12, 5)),
    ]:
        instant = datetime(2026, 11, 19, hour, minute, tzinfo=timezone.utc)
        nxt = scheduler._next_regular_run_at(cfg, instant)
        assert (nxt.hour, nxt.minute) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("day", [19, 28, 29])
async def test_at_most_one_scheduled_start_per_closed_4h_slot(
    session, monkeypatch, day
):
    monkeypatch.setenv("MULTI_SETUP_ANALYST_ENABLED", "1")
    monkeypatch.setattr(
        scheduler, "scheduling_allowed", AsyncMock(return_value=(True, None))
    )
    cfg = AiAnalysisSettings(
        id=1,
        enabled=True,
        unattended_consent=True,
        interval_minutes=60,
        # Old hourly next_run_at must not prevent the 4H migration.
        next_run_at=datetime(2026, 11, day, 9, 1, tzinfo=timezone.utc),
    )
    session.add(cfg)
    await session.commit()

    clock = [datetime(2026, 11, day, 8, 4, tzinfo=timezone.utc)]
    monkeypatch.setattr(scheduler, "_utc_now", lambda: clock[0])
    assert not await scheduler.should_run_scheduled(session)

    clock[0] = datetime(2026, 11, day, 8, 5, tzinfo=timezone.utc)
    assert await scheduler.should_run_scheduled(session)

    # Simulate a crashed worker: analysis record exists, cfg.last_run_at not set.
    run = AiAnalysisRun(
        trigger="scheduled", status="running", started_at=clock[0],
        created_at=clock[0],
    )
    session.add(run)
    await session.commit()

    clock[0] = datetime(2026, 11, day, 9, 30, tzinfo=timezone.utc)
    assert not await scheduler.should_run_scheduled(session)

    clock[0] = datetime(2026, 11, day, 12, 4, tzinfo=timezone.utc)
    assert not await scheduler.should_run_scheduled(session)

    clock[0] = datetime(2026, 11, day, 12, 5, tzinfo=timezone.utc)
    assert await scheduler.should_run_scheduled(session)


@pytest.mark.asyncio
async def test_same_slot_manual_run_blocks_scheduled_duplicate(session, monkeypatch):
    from app.domain import trading_calendar
    monkeypatch.setenv("MULTI_SETUP_ANALYST_ENABLED", "1")
    monkeypatch.setattr(trading_calendar, "new_entries_allowed", lambda _: True)
    monkeypatch.setattr(
        scheduler, "scheduling_allowed", AsyncMock(return_value=(True, None))
    )
    clock = datetime(2026, 11, 19, 8, 7, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler, "_utc_now", lambda: clock)
    session.add(AiAnalysisSettings(
        id=1, enabled=True, unattended_consent=True,
        interval_minutes=60, last_run_at=clock - timedelta(minutes=2),
    ))
    await session.commit()
    assert not await scheduler.should_run_scheduled(session)


def test_failed_4h_scheduled_run_does_not_retry_15min(monkeypatch):
    monkeypatch.setenv("MULTI_SETUP_ANALYST_ENABLED", "1")
    cfg = AiAnalysisSettings(interval_minutes=60)
    now = datetime(2026, 11, 19, 8, 6, tzinfo=timezone.utc)
    with patch.object(scheduler, "_utc_now", return_value=now):
        scheduler.record_run_outcome(cfg, "failed", "transient_test", trigger="scheduled")
    assert cfg.auto_retry_pending is False
    assert cfg.next_run_at == datetime(2026, 11, 19, 12, 5, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_transient_market_fetch_gets_one_retry(session, monkeypatch):
    monkeypatch.setenv("MULTI_SETUP_ANALYST_ENABLED", "1")
    clock = datetime(2026,11,19,8,10,tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler, "_utc_now", lambda: clock)
    monkeypatch.setattr(scheduler, "scheduling_allowed", AsyncMock(return_value=(True,None)))
    session.add(AiAnalysisSettings(id=1,enabled=True,unattended_consent=True,last_run_at=clock))
    def failed():
        return AiAnalysisRun(trigger="scheduled",status="failed",error_code="market_fetch_failed",
                             started_at=clock-timedelta(minutes=4),completed_at=clock-timedelta(minutes=3))
    session.add(failed())
    await session.commit()
    assert await scheduler.should_run_scheduled(session)
    session.add(failed())
    await session.commit()
    assert not await scheduler.should_run_scheduled(session)
