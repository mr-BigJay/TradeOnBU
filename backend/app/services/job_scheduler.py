from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def bind_scheduler(scheduler: AsyncIOScheduler) -> None:
    global _scheduler
    _scheduler = scheduler


def reschedule_ict_scan(seconds: int) -> None:
    if _scheduler is None:
        logger.warning("Scheduler not bound; scan interval change deferred until restart")
        return
    job = _scheduler.get_job("ict_scan")
    if job is None:
        return
    _scheduler.reschedule_job("ict_scan", trigger="interval", seconds=seconds)
    logger.info("Rescheduled ICT scan interval to %ss", seconds)
