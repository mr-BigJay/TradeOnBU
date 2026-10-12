from __future__ import annotations

import uuid
from dataclasses import dataclass

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
    owner = f"ai-{uuid.uuid4().hex}"
    if not await acquire_analysis_lock(owner, ttl_seconds=900):
        return AiRunOutcome(status="busy", run=None)
    try:
        run = await run_ai_analysis(trigger=trigger)
        return AiRunOutcome(status="ok", run=run)
    finally:
        await release_analysis_lock(owner)


async def execute_scheduled_if_due() -> AiRunOutcome:
    async with SessionLocal() as session:
        if not await should_run_scheduled(session):
            return AiRunOutcome(status="skipped", run=None)
    owner = f"ai-{uuid.uuid4().hex}"
    if not await acquire_analysis_lock(owner, ttl_seconds=900):
        return AiRunOutcome(status="busy", run=None)
    try:
        async with SessionLocal() as session:
            if not await should_run_scheduled(session):
                return AiRunOutcome(status="skipped", run=None)
        run = await run_ai_analysis(trigger="scheduled")
        return AiRunOutcome(status="ok", run=run)
    finally:
        await release_analysis_lock(owner)
