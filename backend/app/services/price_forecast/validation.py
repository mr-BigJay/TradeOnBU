from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any

from app.services.price_forecast.hour_bounds import iso_utc, next_six_hourly_closes


class ForecastValidationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def extract_json_object(text: str) -> dict[str, Any]:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    raw = match.group(1) if match else text.strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ForecastValidationError("invalid_json", str(exc)) from exc
    if not isinstance(data, dict):
        raise ForecastValidationError("invalid_json", "Root must be an object")
    return data


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ForecastValidationError("invalid_timestamp", "Missing time")
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ForecastValidationError("invalid_timestamp", str(exc)) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validate_forecast_payload(
    data: dict[str, Any],
    *,
    anchor_price: float,
    expected_times: list[datetime] | None = None,
    symbol: str = "BTCUSDT",
) -> dict[str, Any]:
    expected_times = expected_times or next_six_hourly_closes()
    expected_iso = [iso_utc(t) for t in expected_times]

    if str(data.get("symbol", symbol)).upper() != symbol.upper():
        raise ForecastValidationError("symbol_mismatch", "Unexpected symbol")
    if str(data.get("interval", "1h")).lower() != "1h":
        raise ForecastValidationError("interval_mismatch", "interval must be 1h")
    if int(data.get("horizon_hours", 6)) != 6:
        raise ForecastValidationError("horizon_mismatch", "horizon_hours must be 6")

    ap = data.get("anchor_price")
    try:
        anchor = float(ap)
    except (TypeError, ValueError):
        raise ForecastValidationError("invalid_anchor", "anchor_price required") from None
    if anchor <= 0:
        raise ForecastValidationError("invalid_anchor", "anchor_price must be positive")

    points_raw = data.get("forecast_points")
    if not isinstance(points_raw, list) or len(points_raw) != 6:
        raise ForecastValidationError("point_count", "Exactly six forecast_points required")

    normalized: list[dict[str, Any]] = []
    seen_times: set[str] = set()
    for idx, pt in enumerate(points_raw):
        if not isinstance(pt, dict):
            raise ForecastValidationError("invalid_point", f"Point {idx} not an object")
        t = _parse_time(pt.get("time"))
        t_iso = iso_utc(t)
        if t_iso not in expected_iso:
            raise ForecastValidationError(
                "timestamp_mismatch",
                f"Point {idx} time {t_iso} not in expected hourly boundaries",
            )
        if t_iso in seen_times:
            raise ForecastValidationError("duplicate_timestamp", f"Duplicate time {t_iso}")
        seen_times.add(t_iso)
        try:
            price = float(pt.get("price"))
        except (TypeError, ValueError):
            raise ForecastValidationError("invalid_price", f"Point {idx} price invalid") from None
        if not math.isfinite(price) or price <= 0:
            raise ForecastValidationError("invalid_price", f"Point {idx} price must be positive and finite")
        normalized.append({"time": t_iso, "price": price})

    normalized.sort(key=lambda p: p["time"])
    return {
        "symbol": symbol.upper(),
        "interval": "1h",
        "horizon_hours": 6,
        "generated_at": data.get("generated_at") or iso_utc(datetime.now(timezone.utc)),
        "anchor_price": anchor,
        "forecast_points": normalized,
    }
