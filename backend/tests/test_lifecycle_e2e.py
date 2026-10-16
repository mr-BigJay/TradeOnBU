from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal, init_db
from app.domain.candles import Candle
from app.domain.setup_lifecycle import (
    ICT_CONFIRMED_LONG,
    INVALIDATED,
    PENDING_ENTRY,
    POSITION_OPEN,
    READY_LONG,
    WAIT_LONG,
)
from app.services.ai_setup_memory import create_setup_from_structured
from app.services.conditional_revision import RevisionImpact
from app.domain.setup_monitor_boundary import first_eligible_bar_open_ms
from app.models import AiProposedSetup, SimPosition, SimPositionSettings
from app.services.market_data import KlineBundle
from app.services.opportunity_monitor import OpportunityMonitorService, run_opportunity_monitor_tick
from app.services.opportunity_monitor_lock import new_monitor_owner, try_acquire_opportunity_monitor_lock
from app.services.position import wallet_service as wallet
from app.services.position.position_monitor import SimPositionMonitorService


def _5m_bar(open_ms: int, *, low: float, high: float, close: float) -> Candle:
    return Candle(time_ms=open_ms, open=close, high=high, low=low, close=close, volume=1.0)


def _build_bundle_from_boundary(boundary_ms: int, steps: list[tuple[float, float, float]]) -> KlineBundle:
    closed = []
    for i, (lo, hi, cl) in enumerate(steps):
        closed.append(_5m_bar(boundary_ms + i * 300_000, low=lo, high=hi, close=cl))
    return KlineBundle(
        closed=closed,
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )


@pytest.mark.asyncio
async def test_full_wait_ready_pending_open_e2e():
    await init_db()
    created = datetime.now(timezone.utc) - timedelta(minutes=30)
    boundary = first_eligible_bar_open_ms(created, "5m")

    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=50_000.0)
        settings = (await session.execute(select(SimPositionSettings))).scalar_one_or_none()
        if not settings:
            session.add(SimPositionSettings(id=1))
        settings = (await session.execute(select(SimPositionSettings))).scalar_one()
        settings.initial_balance_set = True
        settings.auto_open_paper_positions = True
        settings.max_risk_pct_per_position = 100.0
        settings.default_margin_usdt = 50.0
        settings.default_leverage = 10

        setup = AiProposedSetup(
            setup_id="e2e-setup-1",
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
            structured_snapshot={"confirmation_type": "sweep_mss_poi"},
        )
        session.add(setup)
        await session.commit()

    bundle = _build_bundle_from_boundary(boundary, [(98, 102, 100), (99.5, 100.5, 100.2)])
    last_open = bundle.closed[-1].time_ms
    now_ms = last_open + 300_000 + 10_000

    async with SessionLocal() as session:
        mon = OpportunityMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            with patch.object(mon.market, "fetch_ticker", AsyncMock(return_value=(101.0, now_ms))):
                with patch("app.services.opportunity_monitor._utc_now") as mock_now:
                    mock_now.return_value = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
                    with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 10, 600)):
                        summary = await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()

        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "e2e-setup-1"))).scalar_one()
        assert summary["promoted"] >= 1
        assert summary["orders"] >= 1
        assert row.lifecycle_state == PENDING_ENTRY
        pos_count = (await session.execute(select(func.count()).select_from(SimPosition))).scalar_one()
        assert pos_count == 1

        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            with patch("app.services.opportunity_monitor._utc_now") as mock_now2:
                mock_now2.return_value = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
                with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 10, 600)):
                    summary2 = await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()
        assert summary2.get("orders", 0) == 0
        pos_count2 = (await session.execute(select(func.count()).select_from(SimPosition))).scalar_one()
        assert pos_count2 == 1

        pos = (await session.execute(select(SimPosition))).scalar_one()
        pos.limit_entry_price = 100.0
        hit_bar = _5m_bar(last_open + 300_000, low=99.0, high=101.0, close=100.0)
        hit_bundle = KlineBundle(
            closed=bundle.closed + [hit_bar],
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="1m",
        )
        from app.services.position.settings_service import get_or_create_settings
        from app.services.position.position_service import SimPositionService

        settings = await get_or_create_settings(session)
        svc = SimPositionService()
        await svc._fill_entry(session, pos, settings, 100.0, transition_suffix="e2e_limit_fill")
        await session.commit()

        pos = (await session.execute(select(SimPosition))).scalar_one()
        assert pos.status == "open"
        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "e2e-setup-1"))).scalar_one()
        assert row.lifecycle_state == POSITION_OPEN


@pytest.mark.asyncio
async def test_concurrent_monitor_ticks_one_order():
    await init_db()
    async with SessionLocal() as session:
        owner1 = new_monitor_owner()
        owner2 = new_monitor_owner()
        assert await try_acquire_opportunity_monitor_lock(owner1) is True
        assert await try_acquire_opportunity_monitor_lock(owner2) is False
        await session.commit()


@pytest.mark.asyncio
async def test_monitor_lock_exclusive_across_sessions():
    await init_db()
    owner1 = new_monitor_owner()
    owner2 = new_monitor_owner()
    async with SessionLocal() as session1:
        assert await try_acquire_opportunity_monitor_lock(owner1) is True
        await session1.commit()
    async with SessionLocal() as session2:
        assert await try_acquire_opportunity_monitor_lock(owner2) is False
        await session2.commit()
    from app.services.opportunity_monitor_lock import release_opportunity_monitor_lock

    await release_opportunity_monitor_lock(owner1)
    async with SessionLocal() as session2:
        assert await try_acquire_opportunity_monitor_lock(owner2) is True
        await session2.commit()


@pytest.mark.asyncio
async def test_parallel_monitor_ticks_single_order():
    """Two concurrent monitor ticks (separate sessions) must not double-create paper orders."""
    await init_db()
    import asyncio

    created = datetime.now(timezone.utc) - timedelta(minutes=30)
    boundary = first_eligible_bar_open_ms(created, "5m")
    bundle = _build_bundle_from_boundary(boundary, [(98, 102, 100), (99.5, 100.5, 100.2)])
    last_open = bundle.closed[-1].time_ms
    now_ms = last_open + 300_000 + 10_000
    now_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=50_000.0)
        settings = (await session.execute(select(SimPositionSettings))).scalar_one_or_none()
        if not settings:
            session.add(SimPositionSettings(id=1))
        settings = (await session.execute(select(SimPositionSettings))).scalar_one()
        settings.initial_balance_set = True
        settings.auto_open_paper_positions = True
        settings.max_risk_pct_per_position = 100.0
        setup = AiProposedSetup(
            setup_id="dup-order-setup",
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
            conditional_plan={"confirmation_sequence": ["entry_zone_touch"], "confirmation_type": "sweep_mss_poi"},
            structured_snapshot={"confirmation_type": "entry_zone_touch"},
        )
        session.add(setup)
        await session.commit()

    async def _tick():
        async with SessionLocal() as session:
            with patch(
                "app.services.opportunity_monitor.MarketDataService.fetch_kline_bundle",
                AsyncMock(return_value=bundle),
            ):
                with patch("app.services.opportunity_monitor._utc_now", return_value=now_dt):
                    result = await run_opportunity_monitor_tick(session)
            await session.commit()
            return result

    results = await asyncio.gather(_tick(), _tick())
    await asyncio.sleep(0.05)
    async with SessionLocal() as session:
        from sqlalchemy import func

        count = (await session.execute(select(func.count()).select_from(SimPosition))).scalar_one()
        assert count == 1
    assert sum(r.get("orders", 0) for r in results) <= 1


@pytest.mark.asyncio
async def test_expiration_naive_sqlite_datetime():
    await init_db()
    async with SessionLocal() as session:
        setup = AiProposedSetup(
            setup_id="exp-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=WAIT_LONG,
            monitoring_enabled=True,
            monitor_from_ms=0,
            monitor_cursor_ms=-1,
            entry_price=100,
            stop_loss=95,
            take_profit=110,
            invalidation_level=90,
            expires_at=datetime(2020, 1, 1, 0, 0, 0),  # naive UTC stored like SQLite
            confirmation_state={"sequence": ["entry_zone_touch"], "required": ["entry_zone_touch"], "completed": [], "missing": ["entry_zone_touch"], "confirmation_timestamps": {}},
        )
        session.add(setup)
        await session.commit()

        mon = OpportunityMonitorService()
        bundle = KlineBundle(
            closed=[_5m_bar(0, low=99, high=101, close=100)],
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="5m",
        )
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 1, 600)):
                await mon.monitor_setups(session)
        await session.commit()
        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "exp-1"))).scalar_one()
        assert row.lifecycle_state == "EXPIRED"


def _long_structured(**overrides):
    base = {
        "decision": "WAIT",
        "setup_mode": "conditional",
        "potential_direction": "LONG",
        "market_bias": "LONG",
        "conditional_entry_zone_low": 99.0,
        "conditional_entry_zone_high": 101.0,
        "proposed_stop_loss": 94.0,
        "proposed_take_profit": 115.0,
        "structural_invalidation_level": 93.0,
        "confirmation_type": "sweep_mss_poi",
        "confirmation_sequence": ["entry_zone_touch"],
        "expires_hours": 48,
        "trade_reason": "e2e",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_e2e_wait_ict_confirmed_ready_pending():
    await init_db()
    created = datetime.now(timezone.utc) - timedelta(minutes=30)
    boundary = first_eligible_bar_open_ms(created, "5m")
    bundle = _build_bundle_from_boundary(boundary, [(98, 102, 100), (99.5, 100.5, 100.2), (99.2, 100.8, 100.1)])
    last_open = bundle.closed[-1].time_ms
    now_ms_1 = last_open + 300_000 + 10_000
    now_ms_2 = last_open + 600_000 + 10_000

    async with SessionLocal() as session:
        await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=50_000.0)
        settings = (await session.execute(select(SimPositionSettings))).scalar_one_or_none()
        if not settings:
            session.add(SimPositionSettings(id=1))
        settings = (await session.execute(select(SimPositionSettings))).scalar_one()
        settings.initial_balance_set = True
        settings.auto_open_paper_positions = True
        settings.max_risk_pct_per_position = 100.0
        settings.default_margin_usdt = 50.0
        settings.default_leverage = 10

        setup = AiProposedSetup(
            setup_id="e2e-ict-path",
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
            invalidation_level=93.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            confirmation_state={
                "sequence": ["entry_zone_touch"],
                "required": ["entry_zone_touch"],
                "completed": [],
                "missing": ["entry_zone_touch"],
                "confirmation_timestamps": {},
            },
            conditional_plan={"confirmation_sequence": ["entry_zone_touch"]},
            structured_snapshot={"confirmation_type": "sweep_mss_poi"},
        )
        session.add(setup)
        await session.commit()

        mon = OpportunityMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            with patch("app.services.opportunity_monitor._utc_now") as mock_now:
                mock_now.return_value = datetime.fromtimestamp(now_ms_1 / 1000, tz=timezone.utc)
                with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 10, 600)):
                    with patch(
                        "app.services.opportunity_monitor._net_rr_ok",
                        AsyncMock(return_value=(False, 1.55)),
                    ):
                        await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()
        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "e2e-ict-path"))).scalar_one()
        assert row.lifecycle_state == ICT_CONFIRMED_LONG
        assert (await session.execute(select(func.count()).select_from(SimPosition))).scalar_one() == 0

        bundle2 = _build_bundle_from_boundary(
            boundary,
            [(98, 102, 100), (99.5, 100.5, 100.2), (99.2, 100.8, 100.1), (99.0, 100.5, 100.0)],
        )
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle2)):
            with patch.object(mon.market, "fetch_ticker", AsyncMock(return_value=(101.0, now_ms_2))):
                with patch("app.services.opportunity_monitor._utc_now") as mock_now2:
                    mock_now2.return_value = datetime.fromtimestamp(now_ms_2 / 1000, tz=timezone.utc)
                    with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 10, 600)):
                        with patch(
                            "app.services.opportunity_monitor._net_rr_ok",
                            AsyncMock(return_value=(True, 2.25)),
                        ):
                            summary = await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()
        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "e2e-ict-path"))).scalar_one()
        assert row.lifecycle_state == PENDING_ENTRY
        assert summary["orders"] >= 1
        assert (await session.execute(select(func.count()).select_from(SimPosition))).scalar_one() == 1


@pytest.mark.asyncio
async def test_e2e_revision_none_keeps_ict_confirmed():
    await init_db()
    from tests.test_conditional_revision_lifecycle import _short_setup_structured

    async with SessionLocal() as session:
        row, _ = await create_setup_from_structured(
            session, run_id=10, symbol="BTCUSDT", structured=_short_setup_structured()
        )
        from app.domain.setup_lifecycle import ICT_CONFIRMED_SHORT

        row.lifecycle_state = ICT_CONFIRMED_SHORT
        row.confirmation_state = {
            "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "completed": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "missing": [],
            "confirmation_timestamps": {
                "liquidity_sweep": 1000,
                "mss": 2000,
                "entry_zone_touch": 3000,
            },
            "confirmation_evidence": {
                "entry_zone_touch": {
                    "bar_low": 82310.0,
                    "bar_high": 82390.0,
                    "zone_lo": 82300.0,
                    "zone_hi": 82400.0,
                }
            },
        }
        await session.commit()
        row2, meta = await create_setup_from_structured(
            session,
            run_id=11,
            symbol="BTCUSDT",
            structured=_short_setup_structured(trade_reason="repeat"),
        )
        await session.commit()
        assert meta.get("revision_impact") == RevisionImpact.NONE.value
        assert row2.lifecycle_state == ICT_CONFIRMED_SHORT


@pytest.mark.asyncio
async def test_e2e_structural_revision_resets_wait():
    await init_db()
    from app.domain.setup_lifecycle import WAIT_SHORT
    from tests.test_conditional_revision_lifecycle import _short_setup_structured

    async with SessionLocal() as session:
        row, _ = await create_setup_from_structured(
            session, run_id=20, symbol="BTCUSDT", structured=_short_setup_structured()
        )
        from app.domain.setup_lifecycle import ICT_CONFIRMED_SHORT

        row.lifecycle_state = ICT_CONFIRMED_SHORT
        row.confirmation_state = {
            "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "completed": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "missing": [],
            "confirmation_timestamps": {"entry_zone_touch": 1000},
        }
        await session.commit()
        row2, meta = await create_setup_from_structured(
            session,
            run_id=21,
            symbol="BTCUSDT",
            structured=_short_setup_structured(
                structural_invalidation_level=82950.0,
                trade_reason="invalidation moved",
            ),
        )
        await session.commit()
        assert meta.get("revision_impact") == RevisionImpact.STRUCTURAL.value
        assert row2.lifecycle_state == WAIT_SHORT
        assert row2.confirmation_state.get("missing")


@pytest.mark.asyncio
async def test_e2e_invalidation_from_wait():
    await init_db()
    created = datetime.now(timezone.utc) - timedelta(minutes=30)
    boundary = first_eligible_bar_open_ms(created, "5m")
    bundle = _build_bundle_from_boundary(boundary, [(98, 102, 100), (92, 94, 92.5)])
    last_open = bundle.closed[-1].time_ms
    now_ms = last_open + 300_000 + 10_000

    async with SessionLocal() as session:
        setup = AiProposedSetup(
            setup_id="e2e-inv",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=WAIT_LONG,
            monitoring_enabled=True,
            monitor_from_ms=boundary,
            monitor_cursor_ms=boundary - 1,
            created_at=created,
            entry_price=100.0,
            entry_zone_low=99.0,
            entry_zone_high=101.0,
            stop_loss=94.0,
            take_profit=115.0,
            invalidation_level=93.0,
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

        mon = OpportunityMonitorService()
        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(return_value=bundle)):
            with patch("app.services.opportunity_monitor._utc_now") as mock_now:
                mock_now.return_value = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
                with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 10, 600)):
                    summary = await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()
        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "e2e-inv"))).scalar_one()
        assert summary["invalidated"] == 1
        assert row.lifecycle_state == INVALIDATED
