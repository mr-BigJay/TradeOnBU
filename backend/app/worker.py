from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import get_effective_settings, get_settings
from app.db import SessionLocal, init_db
from app.models import MarketTrendSnapshot, Signal, SignalStatus
from app.services.entry_detection import EntryDetectionService
from app.services.market_data import MarketDataService
from app.services.paper_execution import PaperExecutionEngine
from app.services.runtime_settings import get_runtime_settings
from app.services.signal_lifecycle import SignalLifecycleService
from app.services.signal_monitor import SignalMonitorService
from app.services.system_state import release_worker_lock, try_acquire_worker_lock
from app.services.notification_outbox import NotificationOutboxService
from app.services.telegram_notifier import TelegramNotifier
from app.services.trend_engine import TrendEngine

logger = logging.getLogger(__name__)


class BotWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.market = MarketDataService(self.settings)
        self.paper = PaperExecutionEngine(self.settings)
        self.trend_engine = TrendEngine(self.market)
        self.entry = EntryDetectionService(self.market, self.trend_engine, self.paper)
        self.lifecycle = SignalLifecycleService(self.paper)
        self.monitor = SignalMonitorService(self.market, self.lifecycle, self.paper)
        self.outbox = NotificationOutboxService()
        self.telegram = TelegramNotifier(self.settings)
        self._last_bias: str | None = None
        self._owner = f"worker-{uuid.uuid4().hex[:8]}"

    async def run_cycle(self) -> None:
        async with SessionLocal() as lock_session:
            if not await try_acquire_worker_lock(lock_session, self._owner):
                logger.warning("Worker cycle skipped — lock held by another instance")
                return

        try:
            self.settings = get_effective_settings()
            symbol = self.settings.default_symbol
            async with SessionLocal() as session:
                from app.services.settings_runtime import refresh_settings_cache

                await refresh_settings_cache(session)
                self.settings = get_effective_settings()
                runtime = await get_runtime_settings(session)
                if runtime.paper_trading_paused or not runtime.scanning_enabled:
                    logger.info("Worker scan skipped — paused or scanning disabled")
                    return
                ctx = await self.trend_engine.analyze(symbol)
                await self.trend_engine.persist(session, ctx)

                if self._last_bias and self._last_bias != ctx.ict_bias.value:
                    await self.telegram.notify_bias_change(symbol, self._last_bias, ctx.ict_bias.value)
                self._last_bias = ctx.ict_bias.value

                monitor_events = await self.monitor.monitor_open_signals(session, runtime)

                for public_id, evs in monitor_events.items():
                    result = await session.execute(select(Signal).where(Signal.public_id == public_id))
                    sig = result.scalar_one_or_none()
                    if not sig:
                        continue
                    for ev in evs:
                        await self.outbox.enqueue(
                            session,
                            dedupe_key=f"{ev}:{sig.public_id}",
                            event_type=ev,
                            signal=sig,
                        )
                await session.commit()

                candidate = await self.entry.scan(symbol, runtime.min_net_rr)
                if candidate and not candidate.rejected_reason:
                    sig = await self.lifecycle.create_from_candidate(
                        session,
                        candidate,
                        runtime.max_pending_minutes,
                        self.settings.telegram_require_approval,
                        runtime.holding_time_hours,
                    )
                    if sig:
                        await self.outbox.enqueue(
                            session,
                            dedupe_key=f"NEW:{sig.public_id}",
                            event_type="NEW",
                            signal=sig,
                        )
                        await session.commit()
                        await session.refresh(sig)
                        logger.info("Created signal %s", sig.public_id)
                await session.commit()
                await self.outbox.flush_pending(session)
        finally:
            async with SessionLocal() as lock_session:
                await release_worker_lock(lock_session, self._owner)

    async def startup(self) -> None:
        await init_db()


async def run_once() -> None:
    logging.basicConfig(level=logging.INFO)
    worker = BotWorker()
    await worker.startup()
    await worker.run_cycle()
    logger.info("Cycle complete at %s", datetime.now(timezone.utc).isoformat())
