from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db, init_db
from app.models import MarketTrendSnapshot, Signal, SignalEvent, SignalStatus
from app.schemas import (
    ChatGPTImportRequest,
    RuntimeSettingsOut,
    RuntimeSettingsUpdate,
    SignalEventOut,
    SignalOut,
    TelegramApprovalRequest,
    TrendSnapshotOut,
)
from app.services.import_signal import ImportSignalService
from app.services.market_data import MarketDataService
from app.services.paper_execution import PaperExecutionEngine
from app.services.runtime_settings import get_runtime_settings
from app.services.signal_lifecycle import SignalLifecycleService
from app.services.telegram_notifier import TelegramNotifier
from app.services.trend_engine import TrendEngine
from app.worker import BotWorker

logger = logging.getLogger(__name__)
settings = get_settings()
worker = BotWorker()
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path("data").mkdir(exist_ok=True)
    await init_db()
    scheduler.add_job(worker.run_cycle, "interval", seconds=settings.scan_interval_seconds, id="ict_scan")
    scheduler.start()
    asyncio.create_task(worker.run_cycle())
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@app.get("/api/health")
async def health():
    return {"status": "ok", "paper_only": True, "time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/settings/runtime", response_model=RuntimeSettingsOut)
async def read_runtime_settings(session: AsyncSession = Depends(get_db)):
    row = await get_runtime_settings(session)
    return RuntimeSettingsOut(
        max_pending_minutes=row.max_pending_minutes,
        max_missed_entry_pct=row.max_missed_entry_pct,
        holding_time_hours=row.holding_time_hours,
        min_net_rr=row.min_net_rr,
        updated_at=row.updated_at,
    )


@app.put("/api/settings/runtime", response_model=RuntimeSettingsOut)
async def update_runtime_settings(body: RuntimeSettingsUpdate, session: AsyncSession = Depends(get_db)):
    row = await get_runtime_settings(session)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(row, field, value)
    await session.commit()
    await session.refresh(row)
    return RuntimeSettingsOut(
        max_pending_minutes=row.max_pending_minutes,
        max_missed_entry_pct=row.max_missed_entry_pct,
        holding_time_hours=row.holding_time_hours,
        min_net_rr=row.min_net_rr,
        updated_at=row.updated_at,
    )


@app.get("/api/trend/current", response_model=TrendSnapshotOut)
async def current_trend(session: AsyncSession = Depends(get_db)):
    symbol = settings.default_symbol
    result = await session.execute(
        select(MarketTrendSnapshot).where(MarketTrendSnapshot.symbol == symbol).order_by(desc(MarketTrendSnapshot.id)).limit(1)
    )
    snap = result.scalar_one_or_none()
    market = MarketDataService()
    try:
        quote = await market.fetch_live_price(symbol)
        stale = market.is_quote_stale(quote)
        live = quote.price
        last_at = quote.fetched_at
    except Exception:
        live = None
        stale = True
        last_at = None

    if snap is None:
        ctx = await TrendEngine(market).analyze(symbol)
        snap = MarketTrendSnapshot(
            symbol=symbol,
            trend_4h=ctx.trend_4h.value,
            trend_15m=ctx.trend_15m.value,
            trend_5m=ctx.trend_5m.value,
            ict_bias=ctx.ict_bias.value,
            details=ctx.details,
        )
        session.add(snap)
        await session.commit()
        await session.refresh(snap)

    return TrendSnapshotOut(
        symbol=snap.symbol,
        trend_4h=snap.trend_4h,
        trend_15m=snap.trend_15m,
        trend_5m=snap.trend_5m,
        ict_bias=snap.ict_bias,
        details=snap.details,
        recorded_at=snap.recorded_at,
        live_price=live,
        price_stale=stale,
        last_price_at=last_at,
    )


@app.get("/api/signals", response_model=list[SignalOut])
async def list_signals(session: AsyncSession = Depends(get_db)):
    result = await session.execute(select(Signal).order_by(desc(Signal.id)).limit(200))
    return list(result.scalars().all())


@app.get("/api/signals/{public_id}/events", response_model=list[SignalEventOut])
async def signal_events(public_id: str, session: AsyncSession = Depends(get_db)):
    sig_result = await session.execute(select(Signal).where(Signal.public_id == public_id))
    sig = sig_result.scalar_one_or_none()
    if not sig:
        raise HTTPException(404, "Signal not found")
    ev_result = await session.execute(
        select(SignalEvent).where(SignalEvent.signal_id == sig.id).order_by(SignalEvent.id)
    )
    return list(ev_result.scalars().all())


@app.post("/api/signals/import", response_model=SignalOut)
async def import_chatgpt_signal(body: ChatGPTImportRequest, session: AsyncSession = Depends(get_db)):
    runtime = await get_runtime_settings(session)
    market = MarketDataService()
    trend = TrendEngine(market)
    paper = PaperExecutionEngine()
    lifecycle = SignalLifecycleService(paper)
    svc = ImportSignalService(trend, paper, lifecycle)
    sig, err = await svc.validate_and_create(
        session,
        body,
        runtime.min_net_rr,
        runtime.max_pending_minutes,
        runtime.holding_time_hours,
        settings.telegram_require_approval,
    )
    if err:
        raise HTTPException(400, err)
    if not sig:
        raise HTTPException(409, "Duplicate or rejected signal")
    await TelegramNotifier().notify_new_setup(sig)
    return sig


@app.post("/api/signals/telegram-approval")
async def telegram_approval(body: TelegramApprovalRequest, session: AsyncSession = Depends(get_db)):
    result = await session.execute(select(Signal).where(Signal.public_id == body.public_id))
    sig = result.scalar_one_or_none()
    if not sig:
        raise HTTPException(404, "Signal not found")
    lifecycle = SignalLifecycleService()
    if not body.approved:
        await lifecycle.transition(session, sig, SignalStatus.REJECTED.value, "USER_REJECTED")
    else:
        sig.telegram_approved = True
        await lifecycle.log_event(session, sig, "USER_APPROVED", sig.status, sig.status)
    await session.commit()
    return {"ok": True, "status": sig.status}


@app.post("/api/worker/run-once")
async def run_worker_once():
    await worker.run_cycle()
    return {"ok": True}


if frontend_dist.exists():

    @app.get("/")
    async def spa_index():
        return FileResponse(frontend_dist / "index.html")

    @app.get("/assets/{rest:path}")
    async def spa_assets(rest: str):
        path = frontend_dist / "assets" / rest
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)
