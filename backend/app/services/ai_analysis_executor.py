from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import AiAnalysisRun
from app.services.ai_analysis_scheduler import (
    acquire_analysis_lock,
    release_analysis_lock,
    should_run_scheduled,
)
from app.services.ai_ict_analysis import run_ai_analysis


@dataclass
class AiRunOutcome:
    status: str  # ok | busy | skipped
    run: AiAnalysisRun | None = None


async def execute_ai_analysis_with_lock(*, trigger: str) -> AiRunOutcome:
    """Acquire lock in a short DB transaction, run inference outside the lock txn, release in finally."""
    owner = f"ai-{uuid.uuid4().hex}"
    if not await acquire_analysis_lock(owner, ttl_seconds=900):
        return AiRunOutcome(status="busy", run=None)
    try:
        async with SessionLocal() as session:
            run = await run_ai_analysis(session, trigger=trigger)
            await session.commit()
            await session.refresh(run)
            return AiRunOutcome(status="ok", run=run)
    finally:
        await release_analysis_lock(owner)


async def execute_scheduled_if_due() -> AiRunOutcome:
    async with SessionLocal() as session:
        if not await should_run_scheduled(session):
            return AiRunOutcome(status="skipped", run=None)
    return await execute_ai_analysis_with_lock(trigger="scheduled")
