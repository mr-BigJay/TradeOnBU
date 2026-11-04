from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.candles import Candle
from app.services import daily_execution_plan as daily
from app.services.ict_execution_plan import STEP, strict_a_target


def setup_and_bars():
    # 12 bars establish support at 997. Closed sweep, break and retest follow.
    bars = [Candle(1000, 1000, 997, 999, i * STEP) for i in range(24)]
    bars[18] = Candle(999, 1000, 995, 998, 18 * STEP)
    bars[19] = Candle(999, 1004, 998, 1003, 19 * STEP)
    for i in range(20, 24):
        bars[i] = Candle(1003, 1005+i, 999, 1003+i/5, i * STEP)
    row = SimpleNamespace(direction="LONG", entry_price=1000, stop_loss=994,
        invalidation_level=995, take_profit=1030, entry_zone_low=999, entry_zone_high=1001,
        structured_snapshot={}, confirmation_state={"confirmation_timestamps":{
            "liquidity_sweep":18*STEP, "mss":19*STEP, "entry_zone_touch":20*STEP}})
    sim = SimpleNamespace(default_margin_usdt=50, default_leverage=10, maker_fee_rate=.0002,
                          taker_fee_rate=.0005, slippage_bps=1, default_tp_behavior="market")
    return row, bars, sim


def test_report_13_has_no_confirmed_short():
    row, bars, sim = setup_and_bars()
    row.direction, row.entry_price, row.stop_loss, row.take_profit = "SHORT", 76270.1, 76351.8, 75694.2
    row.confirmation_state = {}
    assert strict_a_target(row, sim, bars, 24*STEP) == (None, "ict_structure_unconfirmed")


def test_verified_plan_and_first_obstacle():
    row, bars, sim = setup_and_bars()
    tp, reason = strict_a_target(row, sim, bars, 24*STEP)
    assert reason is None and 1012 < tp < 1030
    assert strict_a_target(row, sim, bars[1:8]+bars[9:], 24*STEP)[1] == "ict_structure_data_gap"
    # Put a confirmed overhead pivot outside the MSS lookback.
    bars[4] = Candle(999, 1010, 997, 999, 4*STEP)
    assert strict_a_target(row, sim, bars, 24*STEP)[1] == "ict_obstacle_before_two_r"


@pytest.mark.asyncio
async def test_card_reads_order_and_blocks_on_data_gap(monkeypatch):
    row, bars, sim = setup_and_bars()
    row.setup_id, row.lifecycle_state = "test", "PENDING_ENTRY"
    row.objective_notes = {"market_data_health":{"ok":True}}
    row.last_validation_result = {"ok":True}
    tp, _ = strict_a_target(row, sim, bars, 24*STEP)
    pos = SimpleNamespace(limit_entry_price=1000, stop_loss=994, take_profit=tp,
                          direction="LONG", extra={"strict_a_plan":True})
    session = AsyncMock()
    session.scalars.return_value = Mock(all=lambda:[row])
    session.get.return_value = sim
    session.scalar.return_value = pos
    monkeypatch.setattr(daily, "strategy_settings", AsyncMock(return_value={daily.A:True}))
    ctx = {"as_of":datetime.now(timezone.utc).isoformat(), "live_price":1002}
    plan = await daily.snapshot_plan(session, "BTCUSDT", ctx)
    assert (plan["entry_price"], plan["stop_loss"], plan["take_profits"]) == (1000,994,[pos.take_profit])
    row.lifecycle_state = "READY_LONG"
    row.objective_notes["market_data_health"]["ok"] = False
    assert (await daily.snapshot_plan(session, "BTCUSDT", ctx))["action"] == "wait"
