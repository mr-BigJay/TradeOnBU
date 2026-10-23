from fastapi import APIRouter, BackgroundTasks, HTTPException
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
