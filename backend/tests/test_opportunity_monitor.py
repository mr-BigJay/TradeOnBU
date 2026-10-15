from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.candles import Candle
from app.domain.setup_lifecycle import READY_LONG, WAIT_LONG
from app.models import AiProposedSetup
from app.services.market_data import KlineBundle
from app.services.opportunity_monitor import OpportunityMonitorService


def _bars(n: int = 25, *, zone_touch: bool = True) -> list[Candle]:
    step = 60_000
    base_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - n * step
    out: list[Candle] = []
    for i in range(n):
        lo, hi = 98.0, 102.0
        if zone_touch and i >= n - 3:
            lo, hi = 99.5, 100.5
        out.append(
            Candle(
                time_ms=base_ms + i * step,
                open=100.0,
                high=hi,
                low=lo,
                close=100.2,
                volume=1.0,
            )
        )
    return out


@pytest.mark.asyncio
async def test_wait_promoted_to_ready_when_confirmations_met():
    await init_db()
    async with SessionLocal() as session:
        setup = AiProposedSetup(
            setup_id="mon-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=WAIT_LONG,
            setup_mode="conditional",
            entry_price=100.0,
            entry_zone_low=99.0,
            entry_zone_high=101.0,
            stop_loss=95.0,
            take_profit=110.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            confirmation_state={"required": ["entry_zone_touch"], "completed": [], "missing": ["entry_zone_touch"]},
            structured_snapshot={},
            monitor_cursor_ms=0,
        )
        session.add(setup)
        await session.commit()

        bundle = KlineBundle(
            closed=_bars(),
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="1m",
        )
        mon = OpportunityMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            with patch("app.services.opportunity_monitor.get_effective_settings") as gs:
                settings = gs.return_value
                settings.default_symbol = "BTCUSDT"
                settings.min_net_rr = 2.0
                settings.market_data_stale_seconds = 3600
                summary = await mon.monitor_setups(session, symbol="BTCUSDT")
            await session.commit()
        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "mon-1"))).scalar_one()
        assert "entry_zone_touch" in (row.confirmation_state or {}).get("completed", [])
        assert row.lifecycle_state in (READY_LONG, WAIT_LONG)
        if row.lifecycle_state == WAIT_LONG:
            assert row.last_rejection_reason in (None, "missing_confirmations", "net_rr_below_min")
