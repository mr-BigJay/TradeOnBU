from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import MarketMicroRollup, PriceForecastPoint, PriceForecastRun, PriceForecastSettings
from app.services.price_forecast.bitunix_funding import parse_bitunix_funding_body
from app.services.price_forecast.cost import estimate_forecast_cost_usd
from app.services.price_forecast.hour_bounds import iso_utc, next_six_hourly_closes
from app.services.price_forecast.lock import acquire_price_forecast_lock, release_price_forecast_lock
from app.services.price_forecast.run_policy import schedule_backoff_after_failure
from app.services.price_forecast.run_service import execute_forecast_with_lock
from app.services.price_forecast.maintenance import reconcile_stale_forecast_runs, schedule_after_daily_limit_reached
from app.services.price_forecast.settings_service import count_billable_runs_today, get_or_create_forecast_settings
from app.services.price_forecast.scheduler import execute_scheduled_forecast_if_due
from app.services.price_forecast.trade_rollup import parse_trade_event_ms, upsert_trade_bucket
from app.services.price_forecast.validation import ForecastValidationError, validate_forecast_payload
from app.services.siwc.responses_client import AnalysisResponse, OpenAIResponsesError


BITUNIX_FUNDING_FIXTURE = {
    "code": 0,
    "msg": "Success",
    "data": [
        {
            "symbol": "ETHUSDT",
            "markPrice": "3000",
            "fundingRate": "0.0001",
            "nextFundingTime": "1792368000000",
            "fundingInterval": 8,
        },
        {
            "symbol": "BTCUSDT",
            "markPrice": "80900",
            "lastPrice": "80897.4",
            "indexPrice": "80934.5",
            "fundingRate": "0.000086",
            "nextFundingTime": "1792368000000",
            "fundingInterval": 8,
        },
    ],
}


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
    out = validate_forecast_payload(
        data,
        anchor_price=100_000.0,
        expected_times=[datetime.fromisoformat(t.replace("Z", "+00:00")) for t in times],
    )
    assert len(out["forecast_points"]) == 6


def test_validation_rejects_nan_price():
    times = [iso_utc(t) for t in next_six_hourly_closes()]
    bad = _sample_forecast_json(100.0, times)
    bad["forecast_points"][0]["price"] = float("nan")
    with pytest.raises(ForecastValidationError):
        validate_forecast_payload(bad, anchor_price=100.0)


def test_bitunix_funding_parses_array_and_symbol():
    out = parse_bitunix_funding_body(BITUNIX_FUNDING_FIXTURE, "BTCUSDT")
    assert out["status"] == "ok"
    assert out["funding_rate"] == pytest.approx(0.000086)
    assert out["mark_price"] == pytest.approx(80900.0)


def test_bitunix_funding_missing_symbol_unavailable():
    body = {"code": 0, "data": [{"symbol": "ETHUSDT", "fundingRate": "0.1", "markPrice": "1"}]}
    out = parse_bitunix_funding_body(body, "BTCUSDT")
    assert out["status"] == "unavailable"


def test_cost_no_double_count_reasoning_tokens():
    cfg = PriceForecastSettings(
        id=1,
        input_price_per_mtok=0.10,
        cached_input_price_per_mtok=0.01,
        output_price_per_mtok=0.50,
    )
    cost = estimate_forecast_cost_usd(
        cfg,
        input_tokens=10_000,
        cached_input_tokens=2_000,
        output_tokens=500,
        reasoning_tokens=200,
    )
    # uncached 8000 @ 0.10/M + cached 2000 @ 0.01/M + output 500 @ 0.50/M
    expected = (8000 / 1e6 * 0.10) + (2000 / 1e6 * 0.01) + (500 / 1e6 * 0.50)
    assert cost == pytest.approx(round(expected, 6))


def test_parse_trade_event_iso8601():
    ms = parse_trade_event_ms({"t": "2026-04-07T05:47:52Z", "v": "1", "s": "buy"})
    assert ms is not None
    assert ms > 1_700_000_000_000


@pytest.mark.asyncio
async def test_trade_bucket_upsert_merges_on_second_flush():
    await init_db()
    bucket = 1_700_000_000_000
    async with SessionLocal() as session:
        await upsert_trade_bucket(session, symbol="BTCUSDT", bucket_start_ms=bucket, buy_volume=1.0, sell_volume=2.0, trade_count=3)
        await session.commit()
    async with SessionLocal() as session:
        await upsert_trade_bucket(session, symbol="BTCUSDT", bucket_start_ms=bucket, buy_volume=0.5, sell_volume=1.0, trade_count=2)
        await session.commit()
        row = (
            await session.execute(
                select(MarketMicroRollup).where(
                    MarketMicroRollup.bucket_start_ms == bucket,
                    MarketMicroRollup.kind == "trade_5m",
                )
            )
        ).scalar_one()
        assert row.payload["buy_volume"] == pytest.approx(1.5)
        assert row.payload["sell_volume"] == pytest.approx(3.0)
        assert row.payload["trade_count"] == 5


@pytest.mark.asyncio
async def test_forecast_lock_prevents_double_acquire():
    await init_db()
    assert await acquire_price_forecast_lock("owner-a")
    assert not await acquire_price_forecast_lock("owner-b")
    await release_price_forecast_lock("owner-a")


@pytest.mark.asyncio
async def test_run_forecast_persists_six_points_and_tokens():
    await init_db()
    times_iso = [iso_utc(t) for t in next_six_hourly_closes()]
    ai_json = _sample_forecast_json(80_000.0, times_iso)
    receipt_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    async def fake_collect(session, *, symbol="BTCUSDT"):
        from app.models import MarketDataSnapshot

        snap = MarketDataSnapshot(
            symbol=symbol,
            payload={
                "live_price": {"price": 80_000.0, "stale": False, "receipt_time_ms": receipt_ms},
                "collected_at": iso_utc(datetime.now(timezone.utc)),
            },
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
                outcome = await execute_forecast_with_lock(trigger="manual", skip_enabled_check=True)
    assert outcome.status == "ok"
    run_pid = outcome.run.public_id
    async with SessionLocal() as session:
        run = (await session.execute(select(PriceForecastRun).where(PriceForecastRun.public_id == run_pid))).scalar_one()
        assert run.input_tokens == 9000
        assert run.anchor_time is not None
        assert int(run.anchor_time.timestamp() * 1000) == pytest.approx(receipt_ms, abs=1000)
        pts = (await session.execute(select(PriceForecastPoint).where(PriceForecastPoint.run_id == run.id))).scalars().all()
        assert len(pts) == 6


@pytest.mark.asyncio
async def test_failed_validation_records_usage_and_backoff():
    await init_db()
    receipt_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    async def fake_collect(session, *, symbol="BTCUSDT"):
        from app.models import MarketDataSnapshot

        snap = MarketDataSnapshot(
            symbol=symbol,
            payload={"live_price": {"price": 80_000.0, "receipt_time_ms": receipt_ms}},
            freshness={"live_price": "ok"},
        )
        session.add(snap)
        await session.flush()
        return snap

    bad_json = {"symbol": "BTCUSDT", "interval": "1h", "horizon_hours": 6, "anchor_price": 80000, "forecast_points": []}
    analysis = AnalysisResponse(
        text=json.dumps(bad_json),
        requested_model="gpt-6-luna",
        requested_reasoning_effort="low",
        response_model="gpt-6-luna",
        response_metadata={"usage": {"input_tokens": 100, "output_tokens": 50, "cached_input_tokens": 0}},
    )

    with patch("app.services.price_forecast.run_service.collect_market_snapshot", fake_collect):
        with patch("app.services.price_forecast.run_service.stream_analysis_response", AsyncMock(return_value=analysis)):
            with patch("app.services.price_forecast.run_service._resolve_openai_token", AsyncMock(return_value="sk-test")):
                outcome = await execute_forecast_with_lock(trigger="scheduled", skip_enabled_check=True)
    assert outcome.status == "failed"
    async with SessionLocal() as session:
        cfg = await get_or_create_forecast_settings(session)
        assert cfg.failure_streak >= 1
        assert cfg.next_run_at is not None
        nxt = cfg.next_run_at.replace(tzinfo=timezone.utc) if cfg.next_run_at.tzinfo is None else cfg.next_run_at
        assert nxt > datetime.now(timezone.utc)
        runs = (await session.execute(select(PriceForecastRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].status == "failed"
        assert runs[0].estimated_cost_usd > 0


@pytest.mark.asyncio
async def test_consecutive_openai_failures_increase_backoff():
    await init_db()
    async with SessionLocal() as session:
        cfg = await get_or_create_forecast_settings(session)
        cfg.failure_streak = 2
        await session.commit()
    t1 = schedule_backoff_after_failure(cfg)
    cfg.failure_streak = 4
    t2 = schedule_backoff_after_failure(cfg)
    assert t2 > t1


@pytest.mark.asyncio
async def test_hour_boundary_frozen_horizon():
    anchor = datetime(2026, 6, 1, 10, 59, tzinfo=timezone.utc)
    frozen = next_six_hourly_closes(anchor)
    assert iso_utc(frozen[0]) == "2026-06-01T11:00:00Z"
    later_validation = validate_forecast_payload(
        _sample_forecast_json(100.0, [iso_utc(t) for t in frozen]),
        anchor_price=100.0,
        expected_times=frozen,
    )
    assert len(later_validation["forecast_points"]) == 6


@pytest.mark.asyncio
async def test_openai_error_applies_backoff_without_success():
    await init_db()

    async def fake_collect(session, *, symbol="BTCUSDT"):
        from app.models import MarketDataSnapshot

        snap = MarketDataSnapshot(
            symbol=symbol,
            payload={"live_price": {"price": 1.0, "receipt_time_ms": 1}},
            freshness={"live_price": "ok"},
        )
        session.add(snap)
        await session.flush()
        return snap

    async def boom(*args, **kwargs):
        raise OpenAIResponsesError("http_error", "rate limit")

    with patch("app.services.price_forecast.run_service.collect_market_snapshot", fake_collect):
        with patch("app.services.price_forecast.run_service.stream_analysis_response", boom):
            with patch("app.services.price_forecast.run_service._resolve_openai_token", AsyncMock(return_value="sk-test")):
                await execute_forecast_with_lock(trigger="scheduled", skip_enabled_check=True)
    async with SessionLocal() as session:
        cfg = await get_or_create_forecast_settings(session)
        nxt = cfg.next_run_at.replace(tzinfo=timezone.utc) if cfg.next_run_at.tzinfo is None else cfg.next_run_at
        assert nxt > datetime.now(timezone.utc) + timedelta(minutes=14)


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


def test_trade_bucket_snapshot_and_subtract_avoids_double_count():
    from app.services.price_forecast.bitunix_trade_ws import BitunixMicroDataCollector

    col = BitunixMicroDataCollector(symbol="BTCUSDT")
    col._trade_buckets[1000]["buy_volume"] = 5.0
    col._trade_buckets[1000]["sell_volume"] = 1.0
    col._trade_buckets[1000]["trade_count"] = 4
    pending = col._snapshot_pending_buckets()
    col._trade_buckets[1000]["buy_volume"] = 7.0
    col._subtract_flushed(pending)
    assert col._trade_buckets[1000]["buy_volume"] == pytest.approx(2.0)
    assert col._trade_buckets[1000]["trade_count"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_flush_trades_retry_after_failed_commit():
    await init_db()
    from app.services.price_forecast.bitunix_trade_ws import BitunixMicroDataCollector

    col = BitunixMicroDataCollector(symbol="BTCUSDT")
    col._trade_buckets[2000]["buy_volume"] = 3.0
    col._trade_buckets[2000]["trade_count"] = 2

    with patch("app.services.price_forecast.bitunix_trade_ws.upsert_trade_bucket", AsyncMock(side_effect=RuntimeError("db"))):
        await col._flush_trades()
    assert col._trade_buckets[2000]["buy_volume"] == pytest.approx(3.0)

    await col._flush_trades()
    async with SessionLocal() as session:
        row = (await session.execute(select(MarketMicroRollup).where(MarketMicroRollup.bucket_start_ms == 2000))).scalar_one()
        assert row.payload["buy_volume"] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_stale_live_price_rejected_before_openai():
    await init_db()

    async def fake_collect(session, *, symbol="BTCUSDT"):
        from app.models import MarketDataSnapshot

        snap = MarketDataSnapshot(
            symbol=symbol,
            payload={"live_price": {"price": 80_000.0, "stale": True, "receipt_time_ms": 1}},
            freshness={"live_price": "stale"},
        )
        session.add(snap)
        await session.flush()
        return snap

    with patch("app.services.price_forecast.run_service.collect_market_snapshot", fake_collect):
        with patch("app.services.price_forecast.run_service.stream_analysis_response", AsyncMock()) as mock_ai:
            with patch("app.services.price_forecast.run_service._resolve_openai_token", AsyncMock(return_value="sk-test")):
                outcome = await execute_forecast_with_lock(trigger="manual", skip_enabled_check=True)
    assert outcome.status == "failed"
    mock_ai.assert_not_called()


def test_horizon_expired_when_first_point_not_in_future():
    from app.services.price_forecast.run_policy import assert_horizon_still_valid

    anchor = datetime(2026, 6, 1, 10, 59, tzinfo=timezone.utc)
    expected = next_six_hourly_closes(anchor)
    with pytest.raises(ForecastValidationError):
        assert_horizon_still_valid(expected, now=expected[0] + timedelta(seconds=1))


@pytest.mark.asyncio
async def test_openai_stream_error_keeps_partial_usage():
    await init_db()
    receipt_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    async def fake_collect(session, *, symbol="BTCUSDT"):
        from app.models import MarketDataSnapshot

        snap = MarketDataSnapshot(
            symbol=symbol,
            payload={"live_price": {"price": 80_000.0, "receipt_time_ms": receipt_ms, "stale": False}},
            freshness={"live_price": "ok"},
        )
        session.add(snap)
        await session.flush()
        return snap

    partial = AnalysisResponse(
        text='{"bad":true}',
        requested_model="gpt-6-luna",
        requested_reasoning_effort="low",
        response_model="gpt-6-luna",
        response_metadata={"usage": {"input_tokens": 50, "output_tokens": 10, "cached_input_tokens": 5}},
    )

    async def boom(*args, **kwargs):
        raise OpenAIResponsesError("response_failed", "fail", partial=partial)

    with patch("app.services.price_forecast.run_service.collect_market_snapshot", fake_collect):
        with patch("app.services.price_forecast.run_service.stream_analysis_response", boom):
            with patch("app.services.price_forecast.run_service._resolve_openai_token", AsyncMock(return_value="sk-test")):
                outcome = await execute_forecast_with_lock(trigger="manual", skip_enabled_check=True)
    assert outcome.status == "failed"
    async with SessionLocal() as session:
        run = (await session.execute(select(PriceForecastRun))).scalar_one()
        assert run.input_tokens == 50
        assert run.output_tokens == 10


@pytest.mark.asyncio
async def test_stale_running_run_reconciled_and_excluded_from_daily_count():
    await init_db()
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    async with SessionLocal() as session:
        cfg = await get_or_create_forecast_settings(session)
        cfg.running_status = "running"
        run = PriceForecastRun(
            public_id="stale-run",
            symbol="BTCUSDT",
            status="running",
            trigger="scheduled",
        )
        session.add(run)
        await session.flush()
        run.created_at = old
        await session.commit()
    async with SessionLocal() as session:
        n = await reconcile_stale_forecast_runs(session)
        await session.commit()
        assert n == 1
        run = (await session.execute(select(PriceForecastRun).where(PriceForecastRun.public_id == "stale-run"))).scalar_one()
        assert run.status == "failed"
        assert run.error_code == "process_interrupted"
        cfg = await get_or_create_forecast_settings(session)
        assert cfg.running_status == "idle"
        # Reconciled run is failed (billable once), not stuck as running forever.
        assert await count_billable_runs_today(session) == 1


@pytest.mark.asyncio
async def test_scheduled_daily_limit_advances_next_run_at():
    await init_db()
    async with SessionLocal() as session:
        cfg = await get_or_create_forecast_settings(session)
        cfg.enabled = True
        cfg.daily_request_limit = 1
        cfg.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        session.add(
            PriceForecastRun(
                public_id="billable",
                symbol="BTCUSDT",
                status="ok",
                trigger="manual",
            )
        )
        await session.commit()
    with patch("app.services.price_forecast.run_service.stream_analysis_response", AsyncMock()) as mock_ai:
        outcome = await execute_scheduled_forecast_if_due()
    assert outcome.status == "skipped"
    assert outcome.message == "daily_limit_reached"
    mock_ai.assert_not_called()
    async with SessionLocal() as session:
        cfg = await get_or_create_forecast_settings(session)
        nxt = cfg.next_run_at.replace(tzinfo=timezone.utc) if cfg.next_run_at.tzinfo is None else cfg.next_run_at
        assert nxt >= schedule_after_daily_limit_reached()


def test_schedule_after_daily_limit_is_next_utc_day():
    now = datetime(2026, 6, 15, 23, 50, tzinfo=timezone.utc)
    nxt = schedule_after_daily_limit_reached(now=now)
    assert nxt == datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_ict_instructions_unchanged():
    from app.services.ai_trader_instructions import ICT_TRADER_INSTRUCTIONS

    assert "LONG" in ICT_TRADER_INSTRUCTIONS
    assert "Price Forecast" not in ICT_TRADER_INSTRUCTIONS
