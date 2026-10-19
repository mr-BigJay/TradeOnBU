"""Defer third same-5m ICT token until five 1m slots close; re-evaluate without look-ahead."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.candle_utils import candle_close_ms
from app.domain.entry_execution import ENTRY_MODE_BALANCED
from app.domain.setup_lifecycle import WAIT_LONG
from app.models import AiProposedSetup, SimPosition
from app.services.balanced_confirmation_engine import (
    apply_balanced_progress_to_state,
    process_balanced_new_closed_1m_bars,
)
from app.services.confirmation_policy import resolve_confirmation_policy
from app.services.market_data import KlineBundle
from app.services.opportunity_monitor import OpportunityMonitorService
from tests.test_confirmation_replay_chronology import (
    STEP1,
    STEP5,
    ZONE_HI,
    ZONE_LO,
    _1m,
    _5m,
    _build_positive_same_5m,
    _prior_flat,
)


def _same_5m_three_token_candles() -> tuple[list, list, object, int, int, int, int, int]:
    """ICT-detectable sweep@m1, mss@m3 intra-5m; touch@m3 deferred until 5m 1m grid fills."""
    t5 = 20_000 * STEP5
    prior = _prior_flat(t5)
    bar_5m = _5m(t5, o=100.0, h=100.8, l=97.5, c=100.4)
    closed_5m = prior + [bar_5m]
    m0, m1, m2, m3, m4 = (t5 + i * STEP1 for i in range(5))
    closed_1m = [
        _1m(m0, o=100.0, h=100.2, l=99.8, c=100.0),
        _1m(m1, o=100.0, h=99.85, l=97.5, c=99.6),
        _1m(m2, o=100.15, h=100.25, l=100.12, c=100.18),
        _1m(m3, o=100.15, h=100.5, l=100.0, c=100.45),
        _1m(m4, o=100.2, h=100.15, l=99.95, c=100.05),
    ]
    return closed_5m, closed_1m, bar_5m, t5, m1, m2, m3, m4


def _run_incremental_through_minute(*, through_open_ms: int) -> dict:
    closed_5m, closed_1m, bar_5m, t5, m1, _m2, _m3, _m4 = _same_5m_three_token_candles()
    boundary = m1
    policy = resolve_confirmation_policy(
        sequence=["liquidity_sweep", "mss", "entry_zone_touch"],
        structured={"confirmation_type": "sweep_mss_poi"},
    )
    state: dict = {"monitor_1m_cursor_ms": boundary - 1}
    cursor = boundary - 1
    for open_ms in [c.time_ms for c in closed_1m if boundary <= c.time_ms <= through_open_ms]:
        now_ms = candle_close_ms(_1m(open_ms, o=0, h=0, l=0, c=0), "1m") + 2_000
        partial = [c for c in closed_1m if c.time_ms <= open_ms]
        new = [
            c
            for c in partial
            if c.time_ms > cursor
            and c.time_ms >= boundary
            and now_ms >= candle_close_ms(c, "1m")
        ]
        if not new:
            continue
        progress = process_balanced_new_closed_1m_bars(
            direction="LONG",
            policy=policy,
            confirmation_state=state,
            new_1m_bars=new,
            closed_5m=closed_5m,
            closed_1m=partial,
            zone_lo=ZONE_LO,
            zone_hi=ZONE_HI,
            invalidation_level=89.0,
            structured={},
            monitor_from_ms=boundary,
            now_ms=now_ms,
        )
        state = apply_balanced_progress_to_state(
            state,
            progress,
            policy=policy,
            monitor_1m_cursor_ms=progress.last_bar_ms,
        )
        cursor = progress.last_bar_ms
    return {
        "state": state,
        "closed_5m": closed_5m,
        "closed_1m": closed_1m,
        "bar_5m": bar_5m,
        "boundary": boundary,
        "policy": policy,
        "t5": t5,
        "m1": m1,
        "m3": _m3,
    }


def test_defers_touch_before_5m_close_and_rolls_back_1m_cursor():
    """Monitor tick before 12:05: third token pending; cursor stays before touch minute."""
    ctx = _run_incremental_through_minute(through_open_ms=20_000 * STEP5 + 3 * STEP1)
    state = ctx["state"]
    m2, m3 = ctx["t5"] + 2 * STEP1, ctx["m3"]
    assert state["confirmation_timestamps"]["liquidity_sweep"] == ctx["m1"]
    assert "entry_zone_touch" not in state["confirmation_timestamps"]
    pending = state.get("pending_chronology_proof")
    assert pending and pending["token"] == "entry_zone_touch"
    assert pending["defer_reason"] == "incomplete_1m_coverage"
    assert pending["bar_1m_ms"] == m3
    assert state["monitor_1m_cursor_ms"] == m2


def test_resolves_pending_touch_after_5m_close_without_advancing_past_deferred_bar():
    ctx = _run_incremental_through_minute(through_open_ms=20_000 * STEP5 + 3 * STEP1)
    state = ctx["state"]
    bar_5m = ctx["bar_5m"]
    now_after = candle_close_ms(bar_5m, "5m") + 3_000
    cursor = state["monitor_1m_cursor_ms"]
    new = [
        c
        for c in ctx["closed_1m"]
        if c.time_ms > cursor and now_after >= candle_close_ms(c, "1m")
    ]
    progress = process_balanced_new_closed_1m_bars(
        direction="LONG",
        policy=ctx["policy"],
        confirmation_state=state,
        new_1m_bars=new,
        closed_5m=ctx["closed_5m"],
        closed_1m=ctx["closed_1m"],
        zone_lo=ZONE_LO,
        zone_hi=ZONE_HI,
        invalidation_level=89.0,
        structured={},
        monitor_from_ms=ctx["boundary"],
        now_ms=now_after,
    )
    assert progress.confirmation_timestamps["entry_zone_touch"] == ctx["m3"]
    assert getattr(progress, "pending_chronology_proof", None) is None


@pytest.mark.asyncio
async def test_monitor_tick_before_and_after_1205_for_chronology_defer():
    await init_db()
    closed_5m, closed_1m, bar_5m, t5, m1, m2, m3, m4 = _same_5m_three_token_candles()
    now_pre = candle_close_ms(_1m(m3, o=0, h=0, l=0, c=0), "1m") + 2_000
    now_post = candle_close_ms(bar_5m, "5m") + 3_000

    async with SessionLocal() as session:
        from app.services.runtime_settings import get_runtime_settings

        rt = await get_runtime_settings(session)
        rt.paper_entry_mode = ENTRY_MODE_BALANCED
        await session.commit()

        from tests.test_opportunity_monitor import _wallet_and_settings

        await _wallet_and_settings(session, auto=False)
        setup = AiProposedSetup(
            setup_id="chrono-mon-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=WAIT_LONG,
            monitoring_enabled=True,
            monitor_from_ms=m1,
            monitor_cursor_ms=bar_5m.time_ms - STEP5,
            entry_price=100.0,
            entry_zone_low=ZONE_LO,
            entry_zone_high=ZONE_HI,
            stop_loss=90.0,
            take_profit=130.0,
            invalidation_level=89.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            confirmation_state={
                "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "completed": [],
                "missing": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "confirmation_timestamps": {},
                "monitor_1m_cursor_ms": m1 - 1,
            },
            structured_snapshot={"confirmation_type": "sweep_mss_poi"},
        )
        session.add(setup)
        await session.commit()

        def _bundle_1m(up_to: int, now_ms: int) -> KlineBundle:
            bars = [c for c in closed_1m if c.time_ms <= up_to and now_ms >= candle_close_ms(c, "1m")]
            return KlineBundle(
                closed=bars,
                forming=None,
                gaps_detected=[],
                gaps_remaining=[],
                data_complete=True,
                interval="1m",
            )

        bundle_5m = KlineBundle(
            closed=closed_5m,
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="5m",
        )
        mon = OpportunityMonitorService()

        async def _fetch_pre(symbol: str, interval: str, limit: int):
            return _bundle_1m(m3, now_pre) if interval == "1m" else bundle_5m

        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(side_effect=_fetch_pre)):
            with patch("app.services.opportunity_monitor._utc_now") as mock_now:
                mock_now.return_value = datetime.fromtimestamp(now_pre / 1000, tz=timezone.utc)
                with patch(
                    "app.services.opportunity_monitor.is_market_data_fresh",
                    return_value=(True, 10, 330),
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
        row = (
            await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "chrono-mon-1"))
        ).scalar_one()
        conf = row.confirmation_state or {}
        assert conf.get("pending_chronology_proof", {}).get("token") == "entry_zone_touch"
        assert conf["monitor_1m_cursor_ms"] == m2

        async def _fetch_post(symbol: str, interval: str, limit: int):
            return _bundle_1m(m4, now_post) if interval == "1m" else bundle_5m

        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(side_effect=_fetch_post)):
            with patch("app.services.opportunity_monitor._utc_now") as mock_now:
                mock_now.return_value = datetime.fromtimestamp(now_post / 1000, tz=timezone.utc)
                with patch(
                    "app.services.opportunity_monitor.is_market_data_fresh",
                    return_value=(True, 10, 330),
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
        row = (
            await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "chrono-mon-1"))
        ).scalar_one()
        conf = row.confirmation_state or {}
        assert conf["confirmation_timestamps"].get("entry_zone_touch") == m3
        assert conf.get("pending_chronology_proof") is None
        pos = (
            await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == "chrono-mon-1"))
        ).scalar_one_or_none()
        assert pos is None
