from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.candles import Candle
from app.domain.setup_lifecycle import READY_LONG, WAIT_LONG
from app.domain.setup_monitor_boundary import first_eligible_bar_open_ms
from app.models import AiProposedSetup
from app.services.market_data import KlineBundle
from app.services.opportunity_monitor import OpportunityMonitorService


def _bars_after_boundary(boundary_ms: int, n: int = 3) -> list[Candle]:
    out: list[Candle] = []
    for i in range(n):
        out.append(
            Candle(
                time_ms=boundary_ms + i * 300_000,
                open=100.0,
                high=100.5,
                low=99.5,
                close=100.2,
                volume=1.0,
            )
        )
    return out


@pytest.mark.asyncio
async def test_wait_promoted_to_ready_when_confirmations_met():
    await init_db()
    created = datetime.now(timezone.utc) - timedelta(minutes=20)
    boundary = first_eligible_bar_open_ms(created, "5m")
    last_bar = boundary + 2 * 300_000
    now_ms = last_bar + 300_000 + 5_000

    async with SessionLocal() as session:
        await _wallet_and_settings(session, auto=False)
        setup = AiProposedSetup(
            setup_id="mon-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=WAIT_LONG,
            setup_mode="conditional",
            monitoring_enabled=True,
            monitor_from_ms=boundary,
            monitor_cursor_ms=boundary - 1,
            created_at=created,
            entry_price=100.0,
            entry_zone_low=99.0,
            entry_zone_high=101.0,
            stop_loss=94.0,
            take_profit=115.0,
            invalidation_level=94.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            confirmation_state={
                "sequence": ["entry_zone_touch"],
                "required": ["entry_zone_touch"],
                "completed": [],
                "missing": ["entry_zone_touch"],
                "confirmation_timestamps": {},
            },
            conditional_plan={"confirmation_sequence": ["entry_zone_touch"]},
            structured_snapshot={},
        )
        session.add(setup)
        await session.commit()

        bundle = KlineBundle(
            closed=_bars_after_boundary(boundary, 3),
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="5m",
        )

        mon = OpportunityMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            with patch("app.services.opportunity_monitor._utc_now") as mock_now:
                mock_now.return_value = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
                with patch(
                    "app.services.opportunity_monitor.is_market_data_fresh",
                    return_value=(True, 30, 330),
                ):
                    await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()

        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "mon-1"))).scalar_one()
        assert row.lifecycle_state == READY_LONG
        assert "entry_zone_touch" in (row.confirmation_state or {}).get("completed", [])


async def _wallet_and_settings(db_session, *, auto: bool = True) -> None:
    from app.models import SimPositionSettings
    from app.services.position import wallet_service as wallet

    await wallet.append_ledger(db_session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=10_000.0)
    row = (await db_session.execute(select(SimPositionSettings))).scalar_one_or_none()
    if not row:
        row = SimPositionSettings(id=1)
        db_session.add(row)
    row.initial_balance_set = True
    row.auto_open_paper_positions = auto
    row.max_risk_pct_per_position = 100.0
