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
from app.services.price_forecast.run_policy import (
    assert_horizon_still_valid,
    is_displayable_forecast_run,
    schedule_backoff_after_failure,
    validate_forecast_config,
)
from app.services.price_forecast.maintenance import (
    reconcile_stale_forecast_runs,
    schedule_after_daily_limit_reached,
    schedule_after_monthly_budget_reached,
)
from app.services.price_forecast.settings_service import (
    count_billable_runs_today,
    get_or_create_forecast_settings,
    month_cost_usd,
    schedule_next_run,
)
from app.services.price_forecast.validation import ForecastValidationError, extract_json_object, validate_forecast_payload
from app.services.settings_store import get_plain, get_secret
from app.services.siwc.responses_client import AnalysisResponse, OpenAIResponsesError, stream_analysis_response


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


def _assert_live_price_fresh(live: dict) -> None:
    if live.get("status") == "unavailable" or live.get("price") is None:
        raise ForecastValidationError("live_price_unavailable", "Live anchor price unavailable")
    if live.get("stale"):
        raise ForecastValidationError("live_price_stale", "Live anchor price is stale")
    receipt_ms = live.get("receipt_time_ms")
    if isinstance(receipt_ms, (int, float)) and receipt_ms > 0:
        age_s = (time.time() * 1000 - float(receipt_ms)) / 1000.0
        if age_s > float(get_settings().market_data_stale_seconds):
            raise ForecastValidationError("live_price_stale", "Live anchor price receipt is too old")


def _anchor_time_from_live(live: dict) -> datetime:
    receipt_ms = live.get("receipt_time_ms")
    if isinstance(receipt_ms, (int, float)) and receipt_ms > 0:
        return datetime.fromtimestamp(int(receipt_ms) / 1000, tz=timezone.utc)
    return datetime.now(timezone.utc)


def _apply_usage_to_run(run: PriceForecastRun, cfg: PriceForecastSettings, analysis: AnalysisResponse | None) -> None:
    if analysis is None:
        return
    inp, cached, out, reasoning = _usage_tokens(analysis.response_metadata or {})
    run.input_tokens = inp
    run.cached_input_tokens = cached
    run.output_tokens = out
    run.reasoning_tokens = reasoning
    run.model = analysis.response_model or run.model or cfg.model
    run.reasoning_effort = analysis.confirmed_reasoning_effort or run.reasoning_effort or cfg.reasoning_effort
    run.estimated_cost_usd = estimate_forecast_cost_usd(
        cfg, input_tokens=inp, cached_input_tokens=cached, output_tokens=out, reasoning_tokens=reasoning
    )


async def _finalize_failure(
    run_public: str,
    *,
    code: str,
    msg: str,
    started: float,
    analysis: AnalysisResponse | None,
) -> ForecastRunOutcome:
    async with SessionLocal() as session:
        cfg = await get_or_create_forecast_settings(session)
        run = (
            await session.execute(select(PriceForecastRun).where(PriceForecastRun.public_id == run_public))
        ).scalar_one_or_none()
        if run:
            run.status = "failed"
            run.error_code = code
            run.error_message = msg[:500]
            run.latency_ms = int((time.monotonic() - started) * 1000)
            if analysis and analysis.text:
                run.raw_response_excerpt = analysis.text[:2000]
            _apply_usage_to_run(run, cfg, analysis)
        cfg.failure_streak = int(cfg.failure_streak or 0) + 1
        cfg.running_status = "error"
        cfg.last_error = msg[:255]
        cfg.next_run_at = schedule_backoff_after_failure(cfg)
        await session.commit()
    return ForecastRunOutcome(status="failed", message=msg)


async def _resolve_openai_token(session: AsyncSession) -> str | None:
    settings = get_effective_settings()
    provider = (await get_plain(session, "ai", "provider")) or settings.ai_provider
    if provider != "openai_api":
        return None
    key = await get_secret(session, "ai", "openai_api_key") or settings.openai_api_key.strip()
    return key or None


# Kept testable for historical regressions; never enabled in deployed runtime.
FORECAST_GENERATION_ENABLED = False


async def run_price_forecast(*, trigger: str, skip_enabled_check: bool = False) -> ForecastRunOutcome:
    if not FORECAST_GENERATION_ENABLED:
        return ForecastRunOutcome(status="skipped", message="price_forecast_retired")
    owner = f"forecast-{uuid.uuid4().hex[:12]}"
    if not await acquire_price_forecast_lock(owner):
        return ForecastRunOutcome(status="busy", message="forecast_locked")

    run_public = str(uuid.uuid4())
    started = time.monotonic()
    analysis: AnalysisResponse | None = None
    frozen_expected: list[datetime] | None = None
    try:
        async with SessionLocal() as session:
            cfg = await get_or_create_forecast_settings(session)
            await reconcile_stale_forecast_runs(session)
            validate_forecast_config(cfg)

            if trigger != "manual" and not skip_enabled_check and not cfg.enabled:
                return ForecastRunOutcome(status="skipped", message="forecast_disabled")

            if await count_billable_runs_today(session) >= int(cfg.daily_request_limit):
                if trigger == "scheduled":
                    cfg.next_run_at = schedule_after_daily_limit_reached()
                    await session.commit()
                return ForecastRunOutcome(status="skipped", message="daily_limit_reached")
            if await month_cost_usd(session) >= float(cfg.monthly_budget_usd):
                if trigger == "scheduled":
                    cfg.next_run_at = schedule_after_monthly_budget_reached()
                    await session.commit()
                return ForecastRunOutcome(status="skipped", message="monthly_budget_reached")

            token = await _resolve_openai_token(session)
            if not token:
                cfg.last_error = "openai_api_key_missing"
                cfg.running_status = "error"
                cfg.next_run_at = schedule_backoff_after_failure(cfg)
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
            _assert_live_price_fresh(live)
            anchor = float(live["price"])
            if anchor <= 0:
                raise ForecastValidationError("live_price_unavailable", "Live anchor price unavailable")

            anchor_time = _anchor_time_from_live(live)
            run.anchor_price = anchor
            run.anchor_time = anchor_time

            frozen_expected = next_six_hourly_closes(anchor_time)
            await session.commit()

        async with SessionLocal() as session:
            cfg = await get_or_create_forecast_settings(session)
            token = await _resolve_openai_token(session)
            if not token:
                raise ForecastValidationError("openai_api_key_missing", "missing key")

            run = (
                await session.execute(select(PriceForecastRun).where(PriceForecastRun.public_id == run_public))
            ).scalar_one()
            snap_id = run.market_snapshot_id
            anchor = float(run.anchor_price or 0)
            expected = frozen_expected or next_six_hourly_closes(run.anchor_time)
            expected_iso = [iso_utc(t) for t in expected]

            from app.models import MarketDataSnapshot

            snap = (
                await session.execute(select(MarketDataSnapshot).where(MarketDataSnapshot.id == snap_id))
            ).scalar_one()

            user_input = build_llm_market_summary(snap, anchor_price=anchor, expected_times_iso=expected_iso)
            user_msg = (
                "Return JSON only for BTCUSDT 1h path forecast.\n"
                f"anchor_price must be {anchor}.\n"
                f"forecast_points times must be exactly: {expected_iso}\n"
                f"Market summary JSON:\n{user_input}"
            )

            try:
                analysis = await stream_analysis_response(
                    token,
                    model=cfg.model or "gpt-6-luna",
                    instructions=PRICE_FORECAST_INSTRUCTIONS,
                    user_input=user_msg,
                    reasoning_effort=(cfg.reasoning_effort or "low"),
                    max_output_tokens=int(cfg.max_output_tokens or 1200),
                    timeout=180.0,
                )
            except OpenAIResponsesError as stream_exc:
                if stream_exc.partial is not None:
                    analysis = stream_exc.partial
                raise
            assert_horizon_still_valid(expected)
            parsed = extract_json_object(analysis.text)
            validated = validate_forecast_payload(parsed, anchor_price=anchor, expected_times=expected)

            run.status = "ok"
            run.generated_at = datetime.now(timezone.utc)
            _apply_usage_to_run(run, cfg, analysis)
            run.latency_ms = int((time.monotonic() - started) * 1000)

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
            cfg.failure_streak = 0
            cfg.next_run_at = schedule_next_run(cfg)
            await session.commit()
            return ForecastRunOutcome(status="ok", run=run)

    except (ForecastValidationError, OpenAIResponsesError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, OpenAIResponsesError) and exc.partial is not None:
            analysis = exc.partial
        code = getattr(exc, "code", None) or ("forecast_failed" if not isinstance(exc, ValueError) else str(exc))
        msg = str(getattr(exc, "message", exc))[:500]
        return await _finalize_failure(run_public, code=str(code), msg=msg, started=started, analysis=analysis)
    except Exception as exc:
        return await _finalize_failure(
            run_public, code="forecast_failed", msg=str(exc)[:500], started=started, analysis=analysis
        )
    finally:
        await release_price_forecast_lock(owner)


async def execute_forecast_with_lock(*, trigger: str, skip_enabled_check: bool = False) -> ForecastRunOutcome:
    return await run_price_forecast(trigger=trigger, skip_enabled_check=skip_enabled_check)


async def latest_successful_run(session: AsyncSession) -> PriceForecastRun | None:
    rows = (
        await session.execute(
            select(PriceForecastRun)
            .where(PriceForecastRun.status == "ok")
            .order_by(desc(PriceForecastRun.generated_at))
            .limit(20)
        )
    ).scalars().all()
    for run in rows:
        points = await load_run_points(session, run.id)
        if is_displayable_forecast_run(run, points):
            return run
    return None


async def load_run_points(session: AsyncSession, run_id: int) -> list[PriceForecastPoint]:
    rows = (
        await session.execute(
            select(PriceForecastPoint)
            .where(PriceForecastPoint.run_id == run_id)
            .order_by(PriceForecastPoint.point_index)
        )
    ).scalars().all()
    return list(rows)
