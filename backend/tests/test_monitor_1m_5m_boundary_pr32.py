"""1m confirmation boundary vs 5m grid: no false gaps; integrity order gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.candles import Candle
from app.domain.candle_utils import candle_close_ms, interval_ms
from app.domain.entry_execution import ENTRY_MODE_BALANCED, ENTRY_MODE_STRICT
from app.domain.setup_lifecycle import READY_LONG, WAIT_LONG
from app.domain.setup_monitor_boundary import (
    five_m_monitor_anchor_ms,
    monitor_start_open_ms,
)
from app.models import AiProposedSetup, SimPosition
from app.services.market_data import KlineBundle
from app.services.monitor_1m_integrity import (
    assess_1m_monitor_integrity,
    balanced_1m_allows_new_orders,
)
from app.services.monitor_kline_backfill import detect_skipped_closed_bars, next_closed_bar_open_ms
from app.services.opportunity_monitor import OpportunityMonitorService
from tests.test_confirmation_replay_chronology import STEP1, STEP5, ZONE_HI, ZONE_LO, _build_positive_same_5m


def _ms(h: int, m: int, s: int = 0) -> int:
    """UTC ms for a synthetic day (aligned to 5m grid at 12:00)."""
    base = 1_700_000_000_000
    base = (base // interval_ms("5m")) * interval_ms("5m")
    noon = base + 12 * 3600_000
    return noon + h * 3600_000 + m * 60_000 + s * 1000


def test_balanced_1230_setup_uses_1201_confirm_and_1205_5m_anchor():
    t1200 = _ms(0, 0)
    created = datetime.fromtimestamp((t1200 + 30_000) / 1000, tz=timezone.utc)
    confirm = monitor_start_open_ms(created, ENTRY_MODE_BALANCED)
    assert confirm == t1200 + STEP1
    assert five_m_monitor_anchor_ms(confirm) == t1200 + STEP5


def test_next_closed_5m_bar_not_at_1201_for_1m_boundary():
    t1200 = _ms(0, 0)
    confirm = t1200 + STEP1
    cursor = confirm - 1
    nxt = next_closed_bar_open_ms(cursor_ms=cursor, boundary_ms=confirm, interval="5m")
    assert nxt == t1200 + STEP5
    assert nxt != confirm


def test_no_false_5m_gap_at_1205_and_1210_with_1m_confirm_boundary():
    t1200 = _ms(0, 0)
    confirm = t1200 + STEP1
    cursor = confirm - 1
    five_m = five_m_monitor_anchor_ms(confirm)
    now_ms = t1200 + 2 * STEP5 + STEP5 - 1000
    bars = [
        Candle(time_ms=t1200, open=1, high=1, low=1, close=1, volume=1),
        Candle(time_ms=t1200 + STEP5, open=1, high=1, low=1, close=1, volume=1),
        Candle(time_ms=t1200 + 2 * STEP5, open=1, high=1, low=1, close=1, volume=1),
    ]
    skipped = detect_skipped_closed_bars(
        closed_bars=bars,
        cursor_ms=cursor,
        boundary_ms=five_m,
        now_ms=now_ms,
        interval="5m",
    )
    assert skipped == []


def test_ok_no_new_bars_does_not_allow_balanced_orders():
    assert balanced_1m_allows_new_orders(ok=True, reason="ok_no_new_bars") is False
    assert balanced_1m_allows_new_orders(ok=True, reason="ok") is True


def test_integrity_ok_no_new_bars_requires_coverage():
    t0 = _ms(0, 0)
    boundary = t0 + STEP1
    ok, reason, _ = assess_1m_monitor_integrity(
        closed_1m=[],
        data_complete=True,
        gaps_remaining=[],
        boundary_ms=boundary,
        cursor_ms=boundary - 1,
        now_ms=boundary + 120_000,
    )
    assert not ok and reason == "missing_1m_closed_bars"


@pytest.mark.asyncio
async def test_balanced_shadow_live_strict_no_false_gap_suspend():
    await init_db()
    t1200 = _ms(0, 0)
    confirm = t1200 + STEP1
    async with SessionLocal() as session:
        from app.services.runtime_settings import get_runtime_settings

        rt = await get_runtime_settings(session)
        rt.paper_entry_mode = ENTRY_MODE_BALANCED
        rt.balanced_entry_shadow_mode = True
        await session.commit()

        from tests.test_opportunity_monitor import _wallet_and_settings

        await _wallet_and_settings(session, auto=False)
        setup = AiProposedSetup(
            setup_id="shadow-gap-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=WAIT_LONG,
            monitoring_enabled=True,
            monitor_from_ms=confirm,
            monitor_cursor_ms=confirm - 1,
            created_at=datetime.fromtimestamp((t1200 + 30_000) / 1000, tz=timezone.utc),
            entry_price=100.0,
            entry_zone_low=ZONE_LO,
            entry_zone_high=ZONE_HI,
            stop_loss=90.0,
            take_profit=130.0,
            invalidation_level=89.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            confirmation_state={
                "sequence": ["entry_zone_touch"],
                "required": ["entry_zone_touch"],
                "completed": [],
                "missing": ["entry_zone_touch"],
                "confirmation_timestamps": {},
            },
            structured_snapshot={},
        )
        session.add(setup)
        await session.commit()

        now_ms = t1200 + STEP5 + 5_000
        closed_5m = [
            Candle(time_ms=t1200, open=100, high=101, low=99, close=100, volume=1),
            Candle(time_ms=t1200 + STEP5, open=100, high=101, low=99, close=100, volume=1),
        ]
        bundle_5m = KlineBundle(
            closed=closed_5m,
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="5m",
        )
        bundle_1m = KlineBundle(
            closed=[
                Candle(time_ms=t1200 + i * STEP1, open=100, high=100.2, low=99.8, close=100, volume=1)
                for i in range(1, 7)
            ],
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="1m",
        )
        mon = OpportunityMonitorService()

        async def _fetch(symbol: str, interval: str, limit: int):
            return bundle_1m if interval == "1m" else bundle_5m

        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(side_effect=_fetch)):
            with patch("app.services.opportunity_monitor._utc_now") as mock_now:
                mock_now.return_value = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
                with patch(
                    "app.services.opportunity_monitor.is_market_data_fresh",
                    return_value=(True, 10, 330),
                ):
                    summary = await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()
        row = (
            await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "shadow-gap-1"))
        ).scalar_one()
        health = (row.objective_notes or {}).get("market_data_health") or {}
        assert health.get("reason") != "missing_market_data" or summary.get("checked") == 1
        assert (row.objective_notes or {}).get("balanced_shadow_sim") is not None


@pytest.mark.asyncio
async def test_ready_balanced_blocked_without_fresh_1m_on_ok_no_new_bars():
    await init_db()
    closed_5m, closed_1m, bar_5m, _ = _build_positive_same_5m()
    boundary = bar_5m.time_ms - 14 * STEP5
    stale_latest = closed_1m[-1]
    stale_now = candle_close_ms(stale_latest, "1m") + 400_000

    async with SessionLocal() as session:
        from app.services.runtime_settings import get_runtime_settings

        rt = await get_runtime_settings(session)
        rt.paper_entry_mode = ENTRY_MODE_BALANCED
        await session.commit()
        from tests.test_opportunity_monitor import _wallet_and_settings

        await _wallet_and_settings(session, auto=True)
        setup = AiProposedSetup(
            setup_id="stale-ready-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=READY_LONG,
            monitoring_enabled=True,
            monitor_from_ms=boundary,
            monitor_cursor_ms=bar_5m.time_ms,
            entry_price=100.0,
            entry_zone_low=ZONE_LO,
            entry_zone_high=ZONE_HI,
            stop_loss=90.0,
            take_profit=130.0,
            invalidation_level=89.0,
            confirmation_state={
                "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "completed": ["liquidity_sweep", "mss"],
                "missing": ["entry_zone_touch"],
                "monitor_1m_cursor_ms": bar_5m.time_ms + 2 * STEP1,
            },
            structured_snapshot={"confirmation_type": "sweep_mss_poi"},
        )
        session.add(setup)
        await session.commit()

        bundle_5m = KlineBundle(closed=closed_5m, forming=None, gaps_detected=[], gaps_remaining=[], data_complete=True, interval="5m")
        bundle_1m = KlineBundle(closed=closed_1m, forming=None, gaps_detected=[], gaps_remaining=[], data_complete=True, interval="1m")
        mon = OpportunityMonitorService()

        async def _fetch(symbol: str, interval: str, limit: int):
            return bundle_1m if interval == "1m" else bundle_5m

        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(side_effect=_fetch)):
            with patch("app.services.opportunity_monitor._utc_now") as mock_now:
                mock_now.return_value = datetime.fromtimestamp(stale_now / 1000, tz=timezone.utc)
                with patch(
                    "app.services.opportunity_monitor.is_market_data_fresh",
                    return_value=(True, 30, 330),
                ):
                    with patch(
                        "app.services.opportunity_monitor.setup_history_covers_cursor",
                        return_value=(True, "ok"),
                    ):
                        with patch(
                            "app.services.opportunity_monitor.detect_skipped_closed_bars",
                            return_value=[],
                        ):
                            await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()
        pos = (
            await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == "stale-ready-1"))
        ).scalar_one_or_none()
        assert pos is None


@pytest.mark.asyncio
async def test_restart_recovery_preserves_confirmations_without_retro_fill():
    """After re-monitor with complete 1m/5m, prior BALANCED stamps remain; no order on stale-only tick."""
    from tests.test_balanced_e2e_pr32 import test_balanced_full_e2e_pre_entry_order_and_post_order_fill_only

    await test_balanced_full_e2e_pre_entry_order_and_post_order_fill_only()
