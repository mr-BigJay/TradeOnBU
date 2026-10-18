from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.db import SessionLocal, init_db
from app.models import PriceForecastPoint, PriceForecastRun
from app.services.price_forecast.hour_bounds import iso_utc, next_six_hourly_closes
from app.services.price_forecast.lock import acquire_price_forecast_lock, release_price_forecast_lock
from app.services.price_forecast.run_service import execute_forecast_with_lock
from app.services.price_forecast.validation import ForecastValidationError, validate_forecast_payload
from app.services.siwc.responses_client import AnalysisResponse


def _sample_forecast_json(anchor: float, times: list[str]) -> dict:
    return {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "horizon_hours": 6,
        "generated_at": iso_utc(datetime.now(timezone.utc)),
        "anchor_price": anchor,
        "forecast_points": [{"time": t, "price": anchor + (i + 1) * 10} for i, t in enumerate(times)],
    }


def test_validation_requires_six_hourly_points():
    times = [iso_utc(t) for t in next_six_hourly_closes(datetime(2026, 1, 1, 10, 30, tzinfo=timezone.utc))]
    data = _sample_forecast_json(100_000.0, times)
    out = validate_forecast_payload(data, anchor_price=100_000.0, expected_times=[datetime.fromisoformat(t.replace("Z", "+00:00")) for t in times])
    assert len(out["forecast_points"]) == 6
    assert all(p["price"] > 0 for p in out["forecast_points"])


def test_validation_rejects_wrong_timestamps():
    times = [iso_utc(t) for t in next_six_hourly_closes()]
    bad = _sample_forecast_json(100.0, times)
    bad["forecast_points"][0]["time"] = "2030-01-01T00:00:00Z"
    with pytest.raises(ForecastValidationError):
        validate_forecast_payload(bad, anchor_price=100.0)


@pytest.mark.asyncio
async def test_forecast_lock_prevents_double_acquire():
    await init_db()
    assert await acquire_price_forecast_lock("owner-a")
    assert not await acquire_price_forecast_lock("owner-b")
    await release_price_forecast_lock("owner-a")
    assert await acquire_price_forecast_lock("owner-b")
    await release_price_forecast_lock("owner-b")


@pytest.mark.asyncio
async def test_run_forecast_persists_six_points_and_tokens():
    await init_db()
    times = next_six_hourly_closes()
    times_iso = [iso_utc(t) for t in times]
    ai_json = _sample_forecast_json(80_000.0, times_iso)

    async def fake_collect(session, *, symbol="BTCUSDT"):
        from app.models import MarketDataSnapshot

        snap = MarketDataSnapshot(
            symbol=symbol,
            payload={"live_price": {"price": 80_000.0, "stale": False}, "collected_at": iso_utc(datetime.now(timezone.utc))},
            freshness={"live_price": "ok"},
        )
        session.add(snap)
        await session.flush()
        return snap

    analysis = AnalysisResponse(
        text=json.dumps(ai_json),
        requested_model="gpt-6-luna",
        requested_reasoning_effort="low",
        response_model="gpt-6-luna",
        response_metadata={"usage": {"input_tokens": 9000, "output_tokens": 400, "cached_input_tokens": 1000, "reasoning_tokens": 200}},
    )

    with patch("app.services.price_forecast.run_service.collect_market_snapshot", fake_collect):
        with patch("app.services.price_forecast.run_service.stream_analysis_response", AsyncMock(return_value=analysis)):
            with patch("app.services.price_forecast.run_service._resolve_openai_token", AsyncMock(return_value="sk-test")):
                outcome = await execute_forecast_with_lock(trigger="manual", force=True)
    assert outcome.status == "ok"
    assert outcome.run is not None
    run_pid = outcome.run.public_id
    async with SessionLocal() as session:
        from sqlalchemy import select

        run = (await session.execute(select(PriceForecastRun).where(PriceForecastRun.public_id == run_pid))).scalar_one()
        assert run.input_tokens == 9000
        assert run.output_tokens == 400
        pts = (await session.execute(select(PriceForecastPoint).where(PriceForecastPoint.run_id == run.id))).scalars().all()
        assert len(pts) == 6


@pytest.mark.asyncio
async def test_chart_route_does_not_invoke_openai():
    await init_db()
    from httpx import ASGITransport, AsyncClient
    from app.config import get_settings
    from app.main import app

    transport = ASGITransport(app=app)
    key = get_settings().dashboard_api_key
    with patch("app.services.siwc.responses_client.stream_analysis_response", AsyncMock()) as mock_ai:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch(
                "app.services.chart_data.ChartDataService.bundle",
                AsyncMock(return_value={"candles": [], "symbol": "BTCUSDT", "interval": "1h"}),
            ):
                resp = await client.get("/api/gui/forecast/chart", headers={"X-API-Key": key})
    assert resp.status_code == 200
    mock_ai.assert_not_called()


@pytest.mark.asyncio
async def test_ict_instructions_unchanged():
    from app.services.ai_trader_instructions import ICT_TRADER_INSTRUCTIONS

    assert "LONG" in ICT_TRADER_INSTRUCTIONS
    assert "Price Forecast" not in ICT_TRADER_INSTRUCTIONS
