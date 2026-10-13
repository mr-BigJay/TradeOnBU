from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import AiAnalysisRun
from app.services.ai_analysis_scheduler import reconcile_stale_running_runs


@pytest.mark.asyncio
async def test_reconcile_marks_old_running_as_failed(session):
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    run = AiAnalysisRun(
        status="running",
        trigger="scheduled",
        is_ai=True,
        started_at=stale,
    )
    session.add(run)
    await session.commit()

    n = await reconcile_stale_running_runs()
    assert n == 1

    async with SessionLocal() as verify:
        row = (await verify.execute(select(AiAnalysisRun).where(AiAnalysisRun.id == run.id))).scalar_one()
    assert row.status == "failed"
    assert row.error_code == "stale_running_reconciled"
    assert row.completed_at is not None


@pytest.mark.asyncio
async def test_reconcile_leaves_recent_running(session):
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    run = AiAnalysisRun(
        status="running",
        trigger="manual",
        is_ai=True,
        started_at=recent,
    )
    session.add(run)
    await session.commit()

    n = await reconcile_stale_running_runs()
    assert n == 0

    async with SessionLocal() as verify:
        row = (await verify.execute(select(AiAnalysisRun).where(AiAnalysisRun.id == run.id))).scalar_one()
    assert row.status == "running"
