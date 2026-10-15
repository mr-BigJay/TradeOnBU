from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.db import SessionLocal, init_db
from app.domain.candles import Candle
from app.domain.setup_lifecycle import READY_LONG, WAIT_LONG
from app.domain.candle_utils import candle_close_ms
from app.domain.setup_monitor_boundary import first_eligible_bar_open_ms
from app.services.market_data import KlineBundle
from tests.conftest import with_immediate_monitor_fields
from tests.test_lifecycle_realistic_pipeline import _sweep_mss_poi_long_bars

INTERVAL_5M = 300_000


@pytest.mark.asyncio
async def test_immediate_long_persists_ready_when_confirmations_already_on_closed_bars():
    await init_db()
    step = INTERVAL_5M
    last_bar_open = int(datetime.now(timezone.utc).timestamp() * 1000) // step * step - step
    persist_at = datetime.fromtimestamp((last_bar_open + step + 2_000) / 1000, tz=timezone.utc)
    closed = _sweep_mss_poi_long_bars(last_bar_open - 14 * step)
    shifted: list[Candle] = []
    for i, c in enumerate(closed):
        shifted.append(
            Candle(
                time_ms=last_bar_open - (len(closed) - 1 - i) * step,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
            )
        )
    closed = shifted
    assert candle_close_ms(closed[-1], "5m") <= int(persist_at.timestamp() * 1000)
    bundle = KlineBundle(
        closed=closed,
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    structured = with_immediate_monitor_fields(
        {
            "decision": "LONG",
            "final_ai_decision": "LONG",
            "setup_mode": "immediate",
            "entry_price": 100.0,
            "entry_zone_low": 99.0,
            "entry_zone_high": 101.0,
            "stop_loss": 94.0,
            "take_profit": 115.0,
            "liquidity_event_clear": True,
            "poi_reaction_clear": True,
            "structural_confirmation_clear": True,
            "logical_invalidation_clear": True,
            "proposed_setup": {"new_setup": True},
        }
    )
    from app.services.ai_setup_memory import create_setup_from_structured

    with patch("app.services.ai_setup_memory._utc", return_value=persist_at):
        with patch("app.services.immediate_confirmation_verify.MarketDataService") as mcls:
            mcls.return_value.fetch_kline_bundle = AsyncMock(return_value=bundle)
            async with SessionLocal() as session:
                row, meta = await create_setup_from_structured(
                    session, run_id=7, symbol="BTCUSDT", structured=structured
                )
                await session.commit()
    assert meta["created"] is True
    assert meta.get("immediate_objective_ready") is True
    assert row.lifecycle_state == READY_LONG
    assert row.setup_mode == "immediate"
    completed = (row.confirmation_state or {}).get("completed") or []
    assert "liquidity_sweep" in completed and "entry_zone_touch" in completed


@pytest.mark.asyncio
async def test_immediate_long_without_closed_confirmations_stays_wait():
    await init_db()
    boundary = first_eligible_bar_open_ms(datetime(2026, 10, 10, 12, 7, tzinfo=timezone.utc), "5m")
    bundle = KlineBundle(
        closed=[Candle(time_ms=boundary, open=100, high=100.2, low=99.8, close=100.1, volume=1.0)],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    structured = with_immediate_monitor_fields(
        {
            "decision": "LONG",
            "setup_mode": "immediate",
            "entry_price": 100.0,
            "entry_zone_low": 99.0,
            "entry_zone_high": 101.0,
            "stop_loss": 94.0,
            "take_profit": 115.0,
            "proposed_setup": {"new_setup": True},
        }
    )
    from app.services.ai_setup_memory import create_setup_from_structured

    with patch("app.services.immediate_confirmation_verify.MarketDataService") as mcls:
        mcls.return_value.fetch_kline_bundle = AsyncMock(return_value=bundle)
        async with SessionLocal() as session:
            row, meta = await create_setup_from_structured(
                session, run_id=8, symbol="BTCUSDT", structured=structured
            )
            await session.commit()
    assert row.lifecycle_state == WAIT_LONG
    assert meta.get("immediate_objective_ready") is False
    assert (row.last_validation_result or {}).get("missing")
