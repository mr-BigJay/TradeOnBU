from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.config import get_effective_settings
from app.db import SessionLocal
from app.models import AiAnalysisRun
from app.services.ai_analysis_scheduler import (
    acquire_analysis_lock,
    claim_scheduled_slot,
    due_scheduled_slot,
    get_or_create_settings,
    release_analysis_lock,
    scheduling_allowed,
)
from app.services.ai_ict_analysis import run_ai_analysis
from app.services.ai_scheduled_market_gate import scheduled_analysis_market_ready
from app.services.market_data import MarketDataService


@dataclass
class AiRunOutcome:
    status: str  # ok | busy | skipped | not_ready
    run: AiAnalysisRun | None = None
    reason: str | None = None


async def execute_ai_analysis_with_lock(*, trigger: str) -> AiRunOutcome:
    owner = f"ai-{uuid.uuid4().hex}"
    if not await acquire_analysis_lock(owner):
        return AiRunOutcome(status="busy", run=None)
    try:
        run = await run_ai_analysis(trigger=trigger)
        return AiRunOutcome(status="ok", run=run)
    finally:
        await release_analysis_lock(owner)


async def execute_scheduled_if_due() -> AiRunOutcome:
    settings = get_effective_settings()
    market = MarketDataService(settings)

    async with SessionLocal() as session:
        await get_or_create_settings(session, autocommit_migration=True)
        cfg = await get_or_create_settings(session)
        allowed, block = await scheduling_allowed(session, cfg)
        if not allowed:
            return AiRunOutcome(status="skipped", run=None, reason=block)
        slot = due_scheduled_slot(cfg)
        if slot is None:
            return AiRunOutcome(status="skipped", run=None)

    ready, gate_reason = await scheduled_analysis_market_ready(market)
    if not ready:
        return AiRunOutcome(status="not_ready", run=None, reason=gate_reason)

    owner = f"ai-{uuid.uuid4().hex}"
    if not await acquire_analysis_lock(owner):
        return AiRunOutcome(status="busy", run=None)

    try:
        async with SessionLocal() as session:
            cfg = await get_or_create_settings(session)
            allowed, block = await scheduling_allowed(session, cfg)
            if not allowed:
                return AiRunOutcome(status="skipped", run=None, reason=block)
            slot = due_scheduled_slot(cfg)
            if slot is None:
                return AiRunOutcome(status="skipped", run=None)
            claim_scheduled_slot(cfg, slot)
            await session.commit()

        run = await run_ai_analysis(trigger="scheduled")
        return AiRunOutcome(status="ok", run=run)
    finally:
        await release_analysis_lock(owner)
