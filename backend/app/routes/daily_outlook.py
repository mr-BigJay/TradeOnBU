import base64
import json
from starlette.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import SessionLocal
from app.models import DailyOutlookRun
from app.services.daily_outlook import (
    dashboard, settings_row, reserve_run, execute_reserved, serialize_run,
)

router = APIRouter(prefix="/api/gui/daily-outlook", tags=["daily-outlook"])


class ScheduleBody(BaseModel):
    enabled: bool
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)


@router.get("")
async def get_dashboard():
    return await dashboard()


@router.get("/runs/{run_id}")
async def get_run(run_id: int):
    async with SessionLocal() as session:
        row = await session.get(DailyOutlookRun, run_id)
        if not row:
            raise HTTPException(404, "گزارش پیدا نشد")
        return serialize_run(row)


@router.put("/settings")
async def save_settings(body: ScheduleBody):
    async with SessionLocal() as session:
        cfg = await settings_row(session)
        cfg.enabled, cfg.hour, cfg.minute = body.enabled, body.hour, body.minute
        await session.commit()
    return await dashboard()


@router.post("/generate", status_code=202)
async def generate(background: BackgroundTasks):
    run_id, owner, status = await reserve_run("manual")
    if status == "analysis_busy":
        raise HTTPException(409, "تحلیل دیگری در حال اجراست؛ پس از پایان آن دوباره تلاش کنید.")
    if status == "already_attempted_today":
        raise HTTPException(409, "برای امروز یک تلاش ثبت شده؛ گزارش روز بعد دوباره قابل تولید است.")
    if not run_id or not owner:
        raise HTTPException(409, "شروع تحلیل ممکن نشد")
    background.add_task(execute_reserved, run_id, owner)
    return {"id": run_id, "status": status}


@router.get("/runs")
async def list_runs(before_id: int | None = Query(default=None, gt=0)):
    from app.config import get_effective_settings
    async with SessionLocal() as session:
        query = select(DailyOutlookRun).where(DailyOutlookRun.symbol == get_effective_settings().default_symbol)
        if before_id is not None:
            query = query.where(DailyOutlookRun.id < before_id)
        rows = (await session.execute(query.order_by(DailyOutlookRun.id.desc()).limit(31))).scalars().all()
        return {"items": [serialize_run(r, detail=False) for r in rows[:30]],
                "next_cursor": rows[29].id if len(rows) > 30 else None}


@router.get("/runs/{run_id}/chart-image")
async def get_chart_image(run_id: int):
    from app.services.daily_outlook_image import render_report_image, chart_snapshot
    async with SessionLocal() as session:
        row = await session.get(DailyOutlookRun, run_id)
        if not row:
            raise HTTPException(404, "گزارش پیدا نشد")
        if row.status != "completed":
            raise HTTPException(409, "تصویر پس از تکمیل گزارش آماده می‌شود")
        payload = {"id": row.id, "symbol": row.symbol, "date": row.report_date,
                   "context": row.context or {}, "report": row.report or {}}
        filename = f"daily-outlook-{row.report_date}-{row.id}.png"
    try:
        overlays = chart_snapshot(payload["context"], payload["report"])
        png = await run_in_threadpool(render_report_image, json.dumps(payload, sort_keys=True, ensure_ascii=False))
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, "دادهٔ ذخیره‌شده برای ترسیم چارت کافی یا معتبر نیست")
    return JSONResponse({"data_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
                         "filename": filename, "nearby": overlays["nearby"], "channel": overlays["channel"]}, headers={"Cache-Control": "private, no-store"})
