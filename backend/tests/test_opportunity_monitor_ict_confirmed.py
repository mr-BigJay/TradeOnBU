"""Integration: ICT_CONFIRMED setups stay on the monitor tick path."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal, init_db
from app.domain.candles import Candle
from app.domain.setup_lifecycle import (
    EXPIRED,
    ICT_CONFIRMED_SHORT,
    INVALIDATED,
    READY_SHORT,
    REASON_ICT_CONFIRMED_BLOCKED_RR,
)
from app.domain.setup_monitor_boundary import first_eligible_bar_open_ms
from app.models import AiProposedSetup, SimPosition
from app.services.market_data import KlineBundle
from app.services.opportunity_monitor import OpportunityMonitorService


def _bar(open_ms: int, *, low: float, high: float, close: float) -> Candle:
    return Candle(time_ms=open_ms, open=close, high=high, low=low, close=close, volume=1.0)


def _bundle(boundary_ms: int, steps: list[tuple[float, float, float]]) -> KlineBundle:
    closed = [_bar(boundary_ms + i * 300_000, low=lo, high=hi, close=cl) for i, (lo, hi, cl) in enumerate(steps)]
    return KlineBundle(
        closed=closed,
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )


def _confirmed_short_setup(*, setup_id: str, boundary: int, cursor_ms: int) -> AiProposedSetup:
    seq = ["liquidity_sweep", "mss", "entry_zone_touch"]
    ts = {"liquidity_sweep": boundary, "mss": boundary + 300_000, "entry_zone_touch": boundary + 600_000}
    return AiProposedSetup(
        setup_id=setup_id,
        run_id=1,
        symbol="BTCUSDT",
        direction="SHORT",
        status="open",
        lifecycle_state=ICT_CONFIRMED_SHORT,
        setup_mode="conditional",
        monitoring_enabled=True,
        monitor_from_ms=boundary,
        monitor_cursor_ms=cursor_ms,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=40),
        entry_price=82365.0,
        entry_zone_low=82300.0,
        entry_zone_high=82400.0,
        stop_loss=82650.0,
        take_profit=81730.0,
        invalidation_level=82800.0,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        last_rejection_reason=REASON_ICT_CONFIRMED_BLOCKED_RR,
        last_validation_result={
            "ok": False,
            "reason": REASON_ICT_CONFIRMED_BLOCKED_RR,
            "ict_confirmed": True,
            "net_rr": 1.64,
        },
        confirmation_state={
            "sequence": seq,
            "required": seq,
            "completed": list(seq),
            "missing": [],
            "confirmation_timestamps": ts,
            "confirmation_evidence": {
                "entry_zone_touch": {
                    "bar_low": 82310.0,
                    "bar_high": 82390.0,
                    "zone_lo": 82300.0,
                    "zone_hi": 82400.0,
                }
            },
        },
        conditional_plan={"confirmation_sequence": seq},
        structured_snapshot={"confirmation_type": "sweep_mss_poi"},
    )


async def _run_monitor(session, bundle: KlineBundle, now_ms: int) -> dict:
    mon = OpportunityMonitorService()
    with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
        with patch("app.services.opportunity_monitor._utc_now") as mock_now:
            mock_now.return_value = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
            with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 10, 600)):
                return await mon.monitor_setups(session, symbol="BTCUSDT")


@pytest.mark.asyncio
async def test_ict_confirmed_still_monitored_on_next_tick():
    await init_db()
    created = datetime.now(timezone.utc) - timedelta(minutes=40)
    boundary = first_eligible_bar_open_ms(created, "5m")
    bar0 = boundary
    bar1 = boundary + 300_000
    bar2 = boundary + 600_000
    bar3 = boundary + 900_000
    now_ms = bar3 + 300_000 + 5_000

    async with SessionLocal() as session:
        session.add(_confirmed_short_setup(setup_id="ict-mon-1", boundary=boundary, cursor_ms=bar2))
        await session.commit()

        bundle = _bundle(boundary, [(82350, 82380, 82360), (82340, 82370, 82355), (82345, 82375, 82350), (82340, 82365, 82355)])
        summary = await _run_monitor(session, bundle, now_ms)
        await session.commit()

        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "ict-mon-1"))).scalar_one()
        assert summary["checked"] == 1
        assert row.lifecycle_state == ICT_CONFIRMED_SHORT
        assert row.monitor_cursor_ms == bar3
        assert row.last_monitor_at is not None


@pytest.mark.asyncio
async def test_ict_confirmed_invalidates_structurally():
    await init_db()
    created = datetime.now(timezone.utc) - timedelta(minutes=40)
    boundary = first_eligible_bar_open_ms(created, "5m")
    bar2 = boundary + 600_000
    bar3 = boundary + 900_000
    now_ms = bar3 + 300_000 + 5_000

    async with SessionLocal() as session:
        session.add(_confirmed_short_setup(setup_id="ict-inv", boundary=boundary, cursor_ms=bar2))
        await session.commit()

        bundle = _bundle(
            boundary,
            [
                (82350, 82380, 82360),
                (82340, 82370, 82355),
                (82345, 82375, 82350),
                (82810, 82900, 82850),
            ],
        )
        summary = await _run_monitor(session, bundle, now_ms)
        await session.commit()

        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "ict-inv"))).scalar_one()
        assert summary["invalidated"] == 1
        assert row.lifecycle_state == INVALIDATED


@pytest.mark.asyncio
async def test_ict_confirmed_expires():
    await init_db()
    created = datetime.now(timezone.utc) - timedelta(minutes=40)
    boundary = first_eligible_bar_open_ms(created, "5m")
    bar2 = boundary + 600_000
    bar3 = boundary + 900_000
    now_ms = bar3 + 300_000 + 5_000

    async with SessionLocal() as session:
        setup = _confirmed_short_setup(setup_id="ict-exp", boundary=boundary, cursor_ms=bar2)
        setup.expires_at = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc) - timedelta(seconds=30)
        session.add(setup)
        await session.commit()

        bundle = _bundle(boundary, [(82350, 82380, 82360), (82340, 82370, 82355), (82345, 82375, 82350), (82340, 82365, 82355)])
        summary = await _run_monitor(session, bundle, now_ms)
        await session.commit()

        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "ict-exp"))).scalar_one()
        assert summary["expired"] == 1
        assert row.lifecycle_state == EXPIRED


@pytest.mark.asyncio
async def test_ict_confirmed_promotes_to_ready_when_net_rr_ok_no_order_until_ready():
    await init_db()
    from app.models import SimPositionSettings
    from app.services.position import wallet_service as wallet

    created = datetime.now(timezone.utc) - timedelta(minutes=40)
    boundary = first_eligible_bar_open_ms(created, "5m")
    bar2 = boundary + 600_000
    bar3 = boundary + 900_000
    now_ms = bar3 + 300_000 + 5_000

    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=10_000.0)
        settings = SimPositionSettings(id=1, initial_balance_set=True, auto_open_paper_positions=False)
        session.add(settings)
        session.add(_confirmed_short_setup(setup_id="ict-rr", boundary=boundary, cursor_ms=bar2))
        await session.commit()

        bundle = _bundle(
            boundary,
            [
                (82350, 82380, 82360),
                (82340, 82370, 82355),
                (82345, 82375, 82350),
                (82340, 82365, 82355),
                (82342, 82368, 82352),
            ],
        )
        now_ms_tick1 = boundary + 4 * 300_000 + 300_000 + 5_000
        now_ms_tick2 = boundary + 5 * 300_000 + 300_000 + 5_000

        with patch("app.services.opportunity_monitor._net_rr_ok", AsyncMock(return_value=(False, 1.5))):
            await _run_monitor(session, bundle, now_ms_tick1)
        await session.commit()
        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "ict-rr"))).scalar_one()
        assert row.lifecycle_state == ICT_CONFIRMED_SHORT
        pos_count = (await session.execute(select(func.count()).select_from(SimPosition))).scalar_one()
        assert pos_count == 0

        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "ict-rr"))).scalar_one()
        extra_open = bundle.closed[-1].time_ms + 300_000
        extra = _bar(extra_open, low=82340, high=82365, close=82350)
        bundle2 = KlineBundle(
            closed=bundle.closed + [extra],
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="5m",
        )
        now_ms_tick2 = extra_open + 300_000 + 5_000
        with patch("app.services.opportunity_monitor._net_rr_ok", AsyncMock(return_value=(True, 2.1))):
            summary = await _run_monitor(session, bundle2, now_ms_tick2)
        await session.commit()
        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "ict-rr"))).scalar_one()
        assert summary["promoted"] >= 1
        assert row.lifecycle_state == READY_SHORT
        pos_count = (await session.execute(select(func.count()).select_from(SimPosition))).scalar_one()
        assert pos_count == 0
