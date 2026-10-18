from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.routes.auth import require_dashboard_access
from app.services.chart_data import ChartDataService
from app.services.price_forecast.accuracy import record_actual_closes_for_due_points
from app.services.price_forecast.run_service import execute_forecast_with_lock, latest_successful_run, load_run_points
from app.services.price_forecast.settings_service import (
    count_runs_today,
    get_or_create_forecast_settings,
    month_cost_usd,
    schedule_next_run,
)

router = APIRouter(prefix="/api/gui/forecast", tags=["price-forecast"])


class ForecastSettingsOut(BaseModel):
    enabled: bool
    interval_minutes: int
    model: str
    reasoning_effort: str
    max_output_tokens: int
    daily_request_limit: int
    monthly_budget_usd: float
    input_price_per_mtok: float
    cached_input_price_per_mtok: float
    output_price_per_mtok: float
    reasoning_price_per_mtok: float
    last_run_at: datetime | None
    last_success_at: datetime | None
    next_run_at: datetime | None
    last_error: str | None
    running_status: str
    runs_today: int
    month_cost_usd: float


class ForecastSettingsUpdate(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = Field(None, ge=15, le=360)
    model: str | None = None
    reasoning_effort: str | None = None
    max_output_tokens: int | None = Field(None, ge=256, le=8000)
    daily_request_limit: int | None = Field(None, ge=1, le=200)
    monthly_budget_usd: float | None = Field(None, ge=0.1, le=500)
    input_price_per_mtok: float | None = None
    cached_input_price_per_mtok: float | None = None
    output_price_per_mtok: float | None = None
    reasoning_price_per_mtok: float | None = None


class ForecastPointOut(BaseModel):
    time: str
    price: float
    actual_close: float | None = None
    error_pct: float | None = None


class ForecastChartOut(BaseModel):
    symbol: str
    interval: str
    candles: list[dict]
    anchor_price: float | None
    anchor_time: str | None
    forecast_points: list[ForecastPointOut]
    generated_at: str | None
    run_id: str | None
    collector_freshness: dict | None = None
    error: str | None = None


@router.get("/status", response_model=ForecastSettingsOut)
async def forecast_status(
    session: AsyncSession = Depends(get_db),
    _auth=Depends(require_dashboard_access),
):
    cfg = await get_or_create_forecast_settings(session)
    await session.commit()
    return ForecastSettingsOut(
        enabled=cfg.enabled,
        interval_minutes=cfg.interval_minutes,
        model=cfg.model,
        reasoning_effort=cfg.reasoning_effort,
        max_output_tokens=cfg.max_output_tokens,
        daily_request_limit=cfg.daily_request_limit,
        monthly_budget_usd=cfg.monthly_budget_usd,
        input_price_per_mtok=cfg.input_price_per_mtok,
        cached_input_price_per_mtok=cfg.cached_input_price_per_mtok,
        output_price_per_mtok=cfg.output_price_per_mtok,
        reasoning_price_per_mtok=cfg.reasoning_price_per_mtok,
        last_run_at=cfg.last_run_at,
        last_success_at=cfg.last_success_at,
        next_run_at=cfg.next_run_at,
        last_error=cfg.last_error,
        running_status=cfg.running_status,
        runs_today=await count_runs_today(session),
        month_cost_usd=await month_cost_usd(session),
    )


@router.put("/settings", response_model=ForecastSettingsOut)
async def update_forecast_settings(
    body: ForecastSettingsUpdate,
    session: AsyncSession = Depends(get_db),
    _auth=Depends(require_dashboard_access),
):
    cfg = await get_or_create_forecast_settings(session)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(cfg, field, value)
    if body.enabled is True and cfg.next_run_at is None:
        cfg.next_run_at = schedule_next_run(cfg)
    cfg.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return ForecastSettingsOut(
        enabled=cfg.enabled,
        interval_minutes=cfg.interval_minutes,
        model=cfg.model,
        reasoning_effort=cfg.reasoning_effort,
        max_output_tokens=cfg.max_output_tokens,
        daily_request_limit=cfg.daily_request_limit,
        monthly_budget_usd=cfg.monthly_budget_usd,
        input_price_per_mtok=cfg.input_price_per_mtok,
        cached_input_price_per_mtok=cfg.cached_input_price_per_mtok,
        output_price_per_mtok=cfg.output_price_per_mtok,
        reasoning_price_per_mtok=cfg.reasoning_price_per_mtok,
        last_run_at=cfg.last_run_at,
        last_success_at=cfg.last_success_at,
        next_run_at=cfg.next_run_at,
        last_error=cfg.last_error,
        running_status=cfg.running_status,
        runs_today=await count_runs_today(session),
        month_cost_usd=await month_cost_usd(session),
    )


@router.get("/chart", response_model=ForecastChartOut)
async def forecast_chart(
    session: AsyncSession = Depends(get_db),
    _auth=Depends(require_dashboard_access),
):
    await record_actual_closes_for_due_points()
    chart = ChartDataService()
    bundle = await chart.bundle(symbol="BTCUSDT", interval="1h", limit=120)
    candles = list(bundle.get("candles") or [])
    run = await latest_successful_run(session)
    if not run:
        return ForecastChartOut(
            symbol="BTCUSDT",
            interval="1h",
            candles=candles,
            anchor_price=None,
            anchor_time=None,
            forecast_points=[],
            generated_at=None,
            run_id=None,
            error="no_successful_forecast",
        )
    points = await load_run_points(session, run.id)
    snap_fresh = None
    if run.market_snapshot_id:
        from sqlalchemy import select
        from app.models import MarketDataSnapshot

        snap = (
            await session.execute(select(MarketDataSnapshot).where(MarketDataSnapshot.id == run.market_snapshot_id))
        ).scalar_one_or_none()
        if snap:
            snap_fresh = snap.freshness
    return ForecastChartOut(
        symbol="BTCUSDT",
        interval="1h",
        candles=candles,
        anchor_price=run.anchor_price,
        anchor_time=run.anchor_time.isoformat() if run.anchor_time else None,
        forecast_points=[
            ForecastPointOut(
                time=pt.target_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                price=pt.forecast_price,
                actual_close=pt.actual_close,
                error_pct=pt.error_pct,
            )
            for pt in points
        ],
        generated_at=run.generated_at.isoformat() if run.generated_at else None,
        run_id=run.public_id,
        collector_freshness=snap_fresh,
    )


@router.post("/generate")
async def generate_forecast(
    _auth=Depends(require_dashboard_access),
):
    outcome = await execute_forecast_with_lock(trigger="manual", force=True)
    if outcome.status == "busy":
        raise HTTPException(status_code=409, detail="forecast_busy")
    if outcome.status == "skipped":
        raise HTTPException(status_code=429, detail=outcome.message or "skipped")
    if outcome.status == "failed":
        raise HTTPException(status_code=502, detail=outcome.message or "forecast_failed")
    run = outcome.run
    return {
        "status": "ok",
        "run_id": run.public_id if run else None,
        "estimated_cost_usd": run.estimated_cost_usd if run else 0,
        "input_tokens": run.input_tokens if run else 0,
        "output_tokens": run.output_tokens if run else 0,
    }
