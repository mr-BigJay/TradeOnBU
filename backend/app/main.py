from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap import validate_settings
from app.config import get_settings
from app.db import SessionLocal, get_db, init_db
from app.middleware.session_security import SessionSecurityMiddleware
from app.routes import bitunix_dashboard as bitunix_dashboard_routes
from app.routes import behdark as behdark_routes
from app.routes import daily_outlook as daily_outlook_routes
from app.routes import auth as auth_routes
from app.routes import settings_gui as settings_gui_routes
from app.routes import chatgpt_ai as chatgpt_ai_routes
from app.routes import siwc_public as siwc_public_routes
from app.routes import position as position_routes
from app.routes import opportunities as opportunities_routes
from app.routes import price_forecast as price_forecast_routes
from app.routes import liquidation_intelligence as liquidation_routes
from app.services.settings_runtime import refresh_settings_cache
from app.models import MarketTrendSnapshot, Signal, SignalEvent, SignalStatus
from app.schemas import (
    ChartBundleOut,
    ChatGPTImportRequest,
    RuntimeSettingsOut,
    RuntimeSettingsUpdate,
    SignalEventOut,
    SignalOut,
    TelegramApprovalRequest,
    TrendSnapshotOut,
)
from app.services.chart_data import ChartDataService
from app.services.import_signal import ImportSignalService
from app.services.market_data import MarketDataService
from app.services.paper_execution import PaperExecutionEngine
from app.services.runtime_settings import get_runtime_settings
from app.services.signal_lifecycle import SignalLifecycleService
from app.services.telegram_approval import TelegramApprovalService
from app.services.job_scheduler import bind_scheduler, reschedule_ict_scan
from app.services.telegram_coordinator import get_telegram_coordinator
from app.services.telegram_notifier import TelegramNotifier
from app.services.ai_first_mode import legacy_rule_scan_enabled
from app.services.trend_engine import TrendEngine
from app.worker import BotWorker

logger = logging.getLogger(__name__)
settings = get_settings()
worker = BotWorker()
scheduler = AsyncIOScheduler()
@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_settings(settings)
    Path("data").mkdir(exist_ok=True)
    await init_db()
    # Retire paid forecast generation and its persisted schedule on every startup.
    # Historical predictions and public market-data tables are preserved.
    from app.services.price_forecast.settings_service import retire_forecast_settings

    async with SessionLocal() as session:
        await retire_forecast_settings(session)
        await session.commit()
    async with SessionLocal() as session:
        try:
            await refresh_settings_cache(session)
            from app.services.runtime_settings import get_runtime_settings

            runtime = await get_runtime_settings(session)
            scan_seconds = runtime.scan_interval_seconds
        except Exception:
            logger.exception("Settings cache refresh skipped")
            scan_seconds = settings.scan_interval_seconds
    if not os.getenv("DISABLE_APP_BACKGROUND"):
        bind_scheduler(scheduler)
        scheduler.add_job(
            worker.run_cycle,
            "interval",
            seconds=scan_seconds,
            id="ict_scan",
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        asyncio.create_task(worker.run_cycle())
        get_telegram_coordinator().start()

        async def _ai_scheduled_tick() -> None:
            from app.services.ai_analysis_executor import execute_scheduled_if_due

            await execute_scheduled_if_due()

        scheduler.add_job(
            _ai_scheduled_tick,
            "interval",
            minutes=1,
            id="ai_ict_scheduled",
            max_instances=1,
            coalesce=True,
        )

        from app.services.daily_outlook import daily_outlook_tick

        scheduler.add_job(
            daily_outlook_tick, "interval", minutes=1,
            id="daily_market_outlook", max_instances=1, coalesce=True,
        )

        async def _opportunity_monitor_tick() -> None:
            from app.services.opportunity_monitor import run_opportunity_monitor_tick

            async with SessionLocal() as session:
                try:
                    await run_opportunity_monitor_tick(session)
                    await session.commit()
                except Exception:
                    logger.exception("Opportunity monitor tick failed")

        opp_seconds = max(30, int(get_settings().opportunity_monitor_interval_seconds))
        scheduler.add_job(
            _opportunity_monitor_tick,
            "interval",
            seconds=opp_seconds,
            id="ict_opportunity_monitor",
            max_instances=1,
            coalesce=True,
        )

        # Public-only market data keeps flowing; no ChatGPT forecast job is scheduled.
        async def _public_market_snapshot_tick() -> None:
            from app.services.price_forecast.public_market_snapshots import persist_public_market_snapshot

            await persist_public_market_snapshot(symbol="BTCUSDT")

        scheduler.add_job(
            _public_market_snapshot_tick,
            "interval",
            minutes=15,
            id="public_market_snapshot",
            max_instances=1,
            coalesce=True,
        )

        from app.services.price_forecast.bitunix_trade_ws import get_micro_collector

        get_micro_collector().start()

        from app.services.liquidation_intelligence.collector import get_liquidation_collector
        from app.services.liquidation_intelligence.startup_reconcile import reconcile_liquidation_sources_on_startup

        async with SessionLocal() as session:
            try:
                await reconcile_liquidation_sources_on_startup(session)
                await session.commit()
            except Exception:
                logger.exception("Liquidation source startup reconcile failed")

        get_liquidation_collector().start()

        async def _liquidation_retention_tick() -> None:
            from app.config import get_settings
            from app.services.liquidation_intelligence.maintenance import run_liquidation_retention_tick

            if not get_settings().liquidation_intelligence_enabled:
                return
            try:
                await run_liquidation_retention_tick()
            except Exception:
                logger.exception("Liquidation retention tick failed")

        scheduler.add_job(
            _liquidation_retention_tick,
            "interval",
            hours=1,
            id="liquidation_retention",
            max_instances=1,
            coalesce=True,
        )
    yield
    if not os.getenv("DISABLE_APP_BACKGROUND"):
        from app.services.price_forecast.bitunix_trade_ws import get_micro_collector
        from app.services.liquidation_intelligence.collector import get_liquidation_collector

        await get_micro_collector().stop()
        await get_liquidation_collector().stop()
    scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(bitunix_dashboard_routes.router)
app.include_router(behdark_routes.router)
app.include_router(daily_outlook_routes.router)
app.include_router(auth_routes.router)
app.include_router(settings_gui_routes.router)
app.include_router(chatgpt_ai_routes.router)
app.include_router(siwc_public_routes.router)
app.include_router(position_routes.router)
app.include_router(opportunities_routes.router)
app.include_router(price_forecast_routes.router)
app.include_router(liquidation_routes.router)
app.add_middleware(SessionSecurityMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["*"],
)

frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "paper_only": True,
        "live_trading": False,
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/market/chart", response_model=ChartBundleOut)
async def market_chart(symbol: str | None = None, interval: str = "5m", direction: str | None = None):
    symbol = symbol or settings.default_symbol
    svc = ChartDataService()
    data = await svc.bundle(symbol, interval, direction=direction)
    return ChartBundleOut(**data)


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
        select(MarketTrendSnapshot)
        .where(MarketTrendSnapshot.symbol == symbol)
        .order_by(desc(MarketTrendSnapshot.id))
        .limit(1)
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

    if snap is None and not legacy_rule_scan_enabled():
        return TrendSnapshotOut(
            symbol=symbol,
            trend_4h="UNKNOWN",
            trend_15m="UNKNOWN",
            trend_5m="UNKNOWN",
            ict_bias="UNKNOWN",
            details={"source": "ai_first_mode", "note": "Rule-engine trend scan disabled; use ChatGPT AI analysis."},
            recorded_at=datetime.now(timezone.utc),
            live_price=live,
            price_stale=stale,
            last_price_at=last_at,
        )

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
    await session.commit()
    return sig


def _approval_http_error(err: str) -> HTTPException:
    if err == "NOT_FOUND":
        return HTTPException(404, "Signal not found")
    if err == "UNAUTHORIZED_USER":
        return HTTPException(403, err)
    if err in ("ALREADY_APPROVED", "ALREADY_CLOSED"):
        return HTTPException(409, err)
    return HTTPException(409, err)


@app.post("/api/signals/telegram-approval")
async def telegram_approval(body: TelegramApprovalRequest, session: AsyncSession = Depends(get_db)):
    svc = TelegramApprovalService()
    if body.approved:
        sig, err = await svc.approve(session, body.public_id, user_id=0, source="dashboard")
        if err:
            raise _approval_http_error(err)
        if not sig or sig.telegram_approved is not True:
            raise HTTPException(409, "Approval could not be confirmed")
    else:
        sig, err = await svc.reject(session, body.public_id, user_id=0, source="dashboard")
        if err:
            raise _approval_http_error(err)
    return {"ok": True, "status": sig.status if sig else None, "telegram_approved": bool(sig and sig.telegram_approved)}


@app.post("/api/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if not settings.effective_telegram_webhook():
        raise HTTPException(503, "Telegram webhook mode is disabled")
    if secret != settings.telegram_webhook_secret:
        raise HTTPException(403, "Invalid webhook secret")
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if header_secret != settings.telegram_webhook_secret:
        raise HTTPException(403, "Invalid Telegram secret token header")
    payload = await request.json()
    if "callback_query" in payload or "message" in payload:
        from app.services.telegram_bot import TelegramBotService

        await TelegramBotService().handle_update(payload)
    return {"ok": True}


@app.post("/api/worker/run-once")
async def run_worker_once():
    await worker.run_cycle()
    return {"ok": True}


if frontend_dist.exists():

    @app.get("/")
    async def spa_index():
        return FileResponse(
            frontend_dist / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/assets/{rest:path}")
    async def spa_assets(rest: str):
        path = frontend_dist / "assets" / rest
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(
            path,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

