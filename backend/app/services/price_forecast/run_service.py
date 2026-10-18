from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_effective_settings, get_settings
from app.db import SessionLocal
from app.models import PriceForecastPoint, PriceForecastRun, PriceForecastSettings
from app.services.price_forecast.cost import estimate_forecast_cost_usd
from app.services.price_forecast.hour_bounds import iso_utc, next_six_hourly_closes
from app.services.price_forecast.instructions import PRICE_FORECAST_INSTRUCTIONS
from app.services.price_forecast.lock import acquire_price_forecast_lock, release_price_forecast_lock
from app.services.price_forecast.market_collector import build_llm_market_summary, collect_market_snapshot
from app.services.price_forecast.settings_service import (
    count_runs_today,
    get_or_create_forecast_settings,
    month_cost_usd,
    schedule_next_run,
)
from app.services.price_forecast.validation import ForecastValidationError, extract_json_object, validate_forecast_payload
from app.services.settings_store import get_plain, get_secret
from app.services.siwc.responses_client import OpenAIResponsesError, stream_analysis_response


@dataclass
class ForecastRunOutcome:
    status: str  # ok | busy | skipped | failed
    run: PriceForecastRun | None = None
    message: str | None = None


def _usage_tokens(meta: dict) -> tuple[int, int, int, int]:
    usage = meta.get("usage") or {}
    inp = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or usage.get("input_tokens_details", {}).get("cached_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    reasoning = int(usage.get("reasoning_tokens") or usage.get("output_tokens_details", {}).get("reasoning_tokens") or 0)
    return inp, cached, out, reasoning


async def _resolve_openai_token(session: AsyncSession) -> str | None:
    settings = get_effective_settings()
    provider = (await get_plain(session, "ai", "provider")) or settings.ai_provider
    if provider != "openai_api":
        return None
    key = await get_secret(session, "ai", "openai_api_key") or settings.openai_api_key.strip()
    return key or None


async def run_price_forecast(*, trigger: str, force: bool = False) -> ForecastRunOutcome:
    owner = f"forecast-{uuid.uuid4().hex[:12]}"
    if not await acquire_price_forecast_lock(owner):
        return ForecastRunOutcome(status="busy", message="forecast_locked")

    run_public = str(uuid.uuid4())
    started = time.monotonic()
    try:
        async with SessionLocal() as session:
            cfg = await get_or_create_forecast_settings(session)
            if trigger != "manual" and not force and not cfg.enabled:
                return ForecastRunOutcome(status="skipped", message="forecast_disabled")

            if not force:
                if await count_runs_today(session) >= int(cfg.daily_request_limit):
                    return ForecastRunOutcome(status="skipped", message="daily_limit_reached")
                if await month_cost_usd(session) >= float(cfg.monthly_budget_usd):
                    return ForecastRunOutcome(status="skipped", message="monthly_budget_reached")

            token = await _resolve_openai_token(session)
            if not token:
                cfg.last_error = "openai_api_key_missing"
                cfg.running_status = "error"
                await session.commit()
                return ForecastRunOutcome(status="failed", message="openai_api_key_missing")

            cfg.running_status = "running"
            cfg.last_run_at = datetime.now(timezone.utc)
            run = PriceForecastRun(public_id=run_public, symbol="BTCUSDT", status="running", trigger=trigger)
            session.add(run)
            await session.flush()

            snap = await collect_market_snapshot(session, symbol="BTCUSDT")
            run.market_snapshot_id = snap.id

            live = (snap.payload or {}).get("live_price") or {}
            anchor = float(live.get("price") or 0)
            if anchor <= 0:
                raise ForecastValidationError("live_price_unavailable", "Live anchor price unavailable")

            expected = next_six_hourly_closes()
            expected_iso = [iso_utc(t) for t in expected]
            user_input = build_llm_market_summary(snap, anchor_price=anchor, expected_times_iso=expected_iso)
            user_msg = (
                "Return JSON only for BTCUSDT 1h path forecast.\n"
                f"anchor_price must be {anchor}.\n"
                f"forecast_points times must be exactly: {expected_iso}\n"
                f"Market summary JSON:\n{user_input}"
            )

            analysis = await stream_analysis_response(
                token,
                model=cfg.model or "gpt-6-luna",
                instructions=PRICE_FORECAST_INSTRUCTIONS,
                user_input=user_msg,
                reasoning_effort=(cfg.reasoning_effort or "low"),
                max_output_tokens=int(cfg.max_output_tokens or 1200),
                timeout=180.0,
            )
            parsed = extract_json_object(analysis.text)
            validated = validate_forecast_payload(parsed, anchor_price=anchor, expected_times=expected)

            inp, cached, out, reasoning = _usage_tokens(analysis.response_metadata or {})
            cost = estimate_forecast_cost_usd(cfg, input_tokens=inp, cached_input_tokens=cached, output_tokens=out, reasoning_tokens=reasoning)
            latency = int((time.monotonic() - started) * 1000)

            run.status = "ok"
            run.anchor_price = anchor
            run.anchor_time = datetime.now(timezone.utc)
            run.generated_at = datetime.now(timezone.utc)
            run.model = analysis.response_model or cfg.model
            run.reasoning_effort = analysis.confirmed_reasoning_effort or cfg.reasoning_effort
            run.input_tokens = inp
            run.cached_input_tokens = cached
            run.output_tokens = out
            run.reasoning_tokens = reasoning
            run.latency_ms = latency
            run.estimated_cost_usd = cost

            for idx, pt in enumerate(validated["forecast_points"]):
                target = datetime.fromisoformat(pt["time"].replace("Z", "+00:00"))
                session.add(
                    PriceForecastPoint(
                        run_id=run.id,
                        point_index=idx,
                        target_time=target,
                        forecast_price=float(pt["price"]),
                    )
                )

            cfg.last_success_at = run.generated_at
            cfg.last_error = None
            cfg.running_status = "idle"
            cfg.next_run_at = schedule_next_run(cfg)
            await session.commit()
            return ForecastRunOutcome(status="ok", run=run)

    except (ForecastValidationError, OpenAIResponsesError, json.JSONDecodeError) as exc:
        code = getattr(exc, "code", "forecast_failed")
        msg = str(getattr(exc, "message", exc))[:500]
        async with SessionLocal() as session:
            cfg = await get_or_create_forecast_settings(session)
            run = (
                await session.execute(select(PriceForecastRun).where(PriceForecastRun.public_id == run_public))
            ).scalar_one_or_none()
            if run:
                run.status = "failed"
                run.error_code = code
                run.error_message = msg
                run.latency_ms = int((time.monotonic() - started) * 1000)
            cfg.running_status = "error"
            cfg.last_error = msg[:255]
            await session.commit()
        return ForecastRunOutcome(status="failed", message=msg)
    except Exception as exc:
        async with SessionLocal() as session:
            cfg = await get_or_create_forecast_settings(session)
            cfg.running_status = "error"
            cfg.last_error = str(exc)[:255]
            await session.commit()
        return ForecastRunOutcome(status="failed", message=str(exc))
    finally:
        await release_price_forecast_lock(owner)


async def execute_forecast_with_lock(*, trigger: str, force: bool = False) -> ForecastRunOutcome:
    return await run_price_forecast(trigger=trigger, force=force)


async def latest_successful_run(session: AsyncSession) -> PriceForecastRun | None:
    return (
        await session.execute(
            select(PriceForecastRun)
            .where(PriceForecastRun.status == "ok")
            .order_by(desc(PriceForecastRun.generated_at))
            .limit(1)
        )
    ).scalar_one_or_none()


async def load_run_points(session: AsyncSession, run_id: int) -> list[PriceForecastPoint]:
    rows = (
        await session.execute(
            select(PriceForecastPoint)
            .where(PriceForecastPoint.run_id == run_id)
            .order_by(PriceForecastPoint.point_index)
        )
    ).scalars().all()
    return list(rows)
