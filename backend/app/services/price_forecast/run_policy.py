from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import PriceForecastPoint, PriceForecastRun, PriceForecastSettings

FORECAST_FAILURE_BACKOFF_MINUTES = 15
FORECAST_FAILURE_BACKOFF_CAP_MINUTES = 120

ALLOWED_FORECAST_MODELS = frozenset({"gpt-6-luna"})
MIN_OUTPUT_TOKENS = 256
MAX_OUTPUT_TOKENS = 8000


def validate_forecast_config(cfg: PriceForecastSettings) -> None:
    model = (cfg.model or "").strip()
    if not model:
        raise ValueError("forecast_model_required")
    if model not in ALLOWED_FORECAST_MODELS:
        raise ValueError("forecast_model_not_allowed")
    mot = int(cfg.max_output_tokens or 0)
    if mot < MIN_OUTPUT_TOKENS or mot > MAX_OUTPUT_TOKENS:
        raise ValueError("forecast_max_output_tokens_out_of_range")


def schedule_backoff_after_failure(cfg: PriceForecastSettings, *, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    streak = int(getattr(cfg, "failure_streak", 0) or 0)
    streak = max(1, streak)
    minutes = min(FORECAST_FAILURE_BACKOFF_CAP_MINUTES, FORECAST_FAILURE_BACKOFF_MINUTES * streak)
    return now + timedelta(minutes=minutes)


def forecast_horizon_still_active(points: list[PriceForecastPoint], *, now: datetime | None = None) -> bool:
    if not points:
        return False
    now = now or datetime.now(timezone.utc)
    latest = max(p.target_time.replace(tzinfo=timezone.utc) if p.target_time.tzinfo is None else p.target_time for p in points)
    return latest > now


def is_displayable_forecast_run(run: PriceForecastRun, points: list[PriceForecastPoint]) -> bool:
    if run.status != "ok":
        return False
    return forecast_horizon_still_active(points)
