"""Forecast retirement must stop paid inference while keeping free market collection."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import MarketDataSnapshot, PriceForecastSettings
from app.routes.price_forecast import ForecastSettingsUpdate, generate_forecast, update_forecast_settings
from app.services.price_forecast.public_market_snapshots import persist_public_market_snapshot
from app.services.price_forecast.run_service import run_price_forecast
from app.services.price_forecast.settings_service import retire_forecast_settings


@pytest.mark.asyncio
async def test_direct_forecast_inference_is_disabled_before_lock_or_api():
    with patch(
        "app.services.price_forecast.run_service.acquire_price_forecast_lock",
        side_effect=AssertionError("forecast lock must not be attempted"),
    ):
        outcome = await run_price_forecast(trigger="manual", skip_enabled_check=True)
    assert outcome.status == "skipped"
    assert outcome.message == "price_forecast_retired"


@pytest.mark.asyncio
async def test_old_forecast_routes_cannot_generate_or_enable():
    with pytest.raises(HTTPException) as generated:
        await generate_forecast(_auth=None)
    assert generated.value.status_code == 410
    with pytest.raises(HTTPException) as updated:
        await update_forecast_settings(ForecastSettingsUpdate(enabled=True), session=None, _auth=None)
    assert updated.value.status_code == 410


@pytest.mark.asyncio
async def test_retire_settings_preserves_history_and_turns_off_schedule():
    await init_db()
    async with SessionLocal() as session:
        cfg = await session.get(PriceForecastSettings, 1)
        if cfg is None:
            from app.services.price_forecast.settings_service import get_or_create_forecast_settings
            cfg = await get_or_create_forecast_settings(session)
        cfg.enabled = True
        from datetime import datetime, timezone
        cfg.next_run_at = datetime.now(timezone.utc)
        await session.commit()
        await retire_forecast_settings(session)
        await session.commit()
        assert cfg.enabled is False
        assert cfg.next_run_at is None


@pytest.mark.asyncio
async def test_free_market_snapshots_still_persist_without_openai():
    await init_db()

    async def fake_collector(session, *, symbol):
        session.add(MarketDataSnapshot(
            symbol=symbol,
            payload={"funding": {"status": "ok"}, "open_interest": {"status": "ok"}},
            freshness={"funding": "ok", "open_interest": "ok"},
            valid=True,
        ))
        await session.flush()

    with patch(
        "app.services.price_forecast.public_market_snapshots.collect_market_snapshot",
        new_callable=AsyncMock,
        side_effect=fake_collector,
    ) as collect:
        assert await persist_public_market_snapshot(symbol="BTCUSDT") is True
        collect.assert_awaited_once()

    async with SessionLocal() as session:
        result = await session.execute(select(MarketDataSnapshot).where(MarketDataSnapshot.symbol == "BTCUSDT"))
        assert result.scalars().first() is not None
