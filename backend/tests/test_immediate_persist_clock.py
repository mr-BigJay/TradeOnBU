"""Immediate READY at actual persist time using only already-closed 5m bars."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.db import SessionLocal, init_db
from app.domain.candle_utils import candle_close_ms
from app.domain.candles import Candle
from app.domain.setup_lifecycle import READY_LONG
from app.services.market_data import KlineBundle
from tests.conftest import with_immediate_monitor_fields
from tests.test_lifecycle_realistic_pipeline import _sweep_mss_poi_long_bars


@pytest.mark.asyncio
async def test_immediate_ready_at_persist_from_recently_closed_candles_only():
    await init_db()
    step = 300_000
    # Persist time is shortly after the last replay bar closes (no future bar).
    last_bar_open = 1_765_000_000_000
    persist_at = datetime.fromtimestamp((last_bar_open + step + 2_000) / 1000, tz=timezone.utc)
    closed = _sweep_mss_poi_long_bars(last_bar_open - 14 * step)
    # Shift bar timestamps to end at last_bar_open
    shifted = []
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
    last = shifted[-1]
    assert candle_close_ms(last, "5m") <= int(persist_at.timestamp() * 1000)

    bundle = KlineBundle(
        closed=shifted,
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
            "proposed_setup": {"new_setup": True},
        }
    )
    from app.services.ai_setup_memory import create_setup_from_structured

    with patch("app.services.ai_setup_memory._utc", return_value=persist_at):
        with patch("app.services.immediate_confirmation_verify.MarketDataService") as mcls:
            mcls.return_value.fetch_kline_bundle = AsyncMock(return_value=bundle)
            async with SessionLocal() as session:
                row, meta = await create_setup_from_structured(
                    session, run_id=99, symbol="BTCUSDT", structured=structured
                )
                await session.commit()

    assert meta["immediate_objective_ready"] is True
    assert row.lifecycle_state == READY_LONG
    ts = (row.confirmation_state or {}).get("confirmation_timestamps") or {}
    assert ts, "expected confirmation timestamps from closed history"
    assert all(t <= int(persist_at.timestamp() * 1000) for t in ts.values())
