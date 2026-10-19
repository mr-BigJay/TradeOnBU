from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.candles import Candle
from app.domain.candle_utils import candle_close_ms
from app.domain.entry_execution import ENTRY_MODE_BALANCED, ENTRY_MODE_STRICT
from app.domain.setup_lifecycle import READY_LONG, WAIT_LONG
from app.domain.setup_monitor_boundary import first_eligible_bar_open_ms
from app.models import AiProposedSetup
from app.services.balanced_confirmation_engine import (
    process_balanced_new_closed_1m_bars,
    strict_apply_5m_bar,
)
from app.services.confirmation_policy import resolve_confirmation_policy
from app.services.entry_execution_settings import live_confirmation_engine_mode
from app.services.ict_confirmation_engine import process_bar_sequential
from app.services.market_data import KlineBundle
from app.services.opportunity_miss_report import classify_user_audit_bucket
from app.services.opportunity_monitor import OpportunityMonitorService
from tests.test_confirmation_replay_chronology import (
    STEP1,
    STEP5,
    ZONE_HI,
    ZONE_LO,
    _build_positive_same_5m,
)


def test_balanced_1m_completes_sequence_before_5m_close():
    closed_5m, closed_1m, bar_5m, now_ms = _build_positive_same_5m()
    seq = ["liquidity_sweep", "mss", "entry_zone_touch"]
    policy = resolve_confirmation_policy(sequence=seq, structured={"confirmation_type": "sweep_mss_poi"})
    boundary = closed_5m[0].time_ms
    # Evaluate after minute 3 closes — 5m candle still open.
    m2 = bar_5m.time_ms + 2 * STEP1
    now_mid = candle_close_ms(
        Candle(time_ms=m2, open=0, high=0, low=0, close=0, volume=0), "1m"
    ) + 1_000
    partial_1m = [c for c in closed_1m if c.time_ms <= m2]
    progress = process_balanced_new_closed_1m_bars(
        direction="LONG",
        policy=policy,
        confirmation_state={"confirmation_timestamps": {}, "confirmation_evidence": {}},
        new_1m_bars=partial_1m,
        closed_5m=closed_5m,
        closed_1m=closed_1m,
        zone_lo=ZONE_LO,
        zone_hi=ZONE_HI,
        invalidation_level=90.0,
        structured={},
        monitor_from_ms=boundary,
        now_ms=now_mid,
    )
    assert "liquidity_sweep" in progress.completed
    assert now_mid < candle_close_ms(bar_5m, "5m")


def test_strict_still_one_token_per_closed_5m():
    closed_5m, _closed_1m, bar_5m, _ = _build_positive_same_5m()
    seq = ["liquidity_sweep", "mss", "entry_zone_touch"]
    policy = resolve_confirmation_policy(sequence=seq, structured={"confirmation_type": "sweep_mss_poi"})
    progress = strict_apply_5m_bar(
        direction="LONG",
        policy=policy,
        confirmation_state={"confirmation_timestamps": {}, "confirmation_evidence": {}},
        bar=bar_5m,
        window=closed_5m,
        zone_lo=ZONE_LO,
        zone_hi=ZONE_HI,
        invalidation_level=90.0,
        structured={},
        monitor_from_ms=closed_5m[0].time_ms,
    )
    assert len(progress.completed) == 1


def test_policy_ai_optional_cannot_drop_profile_minimum():
    seq = ["liquidity_sweep", "mss", "entry_zone_touch"]
    policy = resolve_confirmation_policy(
        sequence=seq,
        structured={
            "confirmation_type": "sweep_mss_poi",
            "optional_confirmations": ["liquidity_sweep", "mss"],
        },
    )
    assert "liquidity_sweep" in policy.required
    assert "mss" in policy.required


def test_shadow_live_engine_stays_strict():
    assert live_confirmation_engine_mode(ENTRY_MODE_BALANCED, True) == ENTRY_MODE_STRICT
    assert live_confirmation_engine_mode(ENTRY_MODE_BALANCED, False) == ENTRY_MODE_BALANCED


def test_miss_audit_profit_requires_positive_net_pnl():
    class _Setup:
        objective_notes = {}

    bucket = classify_user_audit_bucket(
        _Setup(),  # type: ignore[arg-type]
        timeline={
            "paper_orders": [
                {"status": "closed", "fill_price": 100.0, "net_pnl_usdt": -5.0, "close_reason": "sl_hit"}
            ]
        },
        execution_outcome="valid_opportunity_order_created",
    )
    from app.domain.entry_execution import AUDIT_EXECUTED_LOSS

    assert bucket == AUDIT_EXECUTED_LOSS


@pytest.mark.asyncio
async def test_balanced_pre_entry_order_before_5m_close_and_no_pre_order_fill():
    """Pre-entry limit only after structural 1m confirms; fill only after order exists."""
    await init_db()
    closed_5m, closed_1m, bar_5m, full_now = _build_positive_same_5m()
    boundary = bar_5m.time_ms - 14 * STEP5
    m3 = bar_5m.time_ms + 3 * STEP1
    now_ms = candle_close_ms(
        Candle(time_ms=m3, open=0, high=0, low=0, close=0, volume=0), "1m"
    ) + 2_000

    async with SessionLocal() as session:
        from app.services.runtime_settings import get_runtime_settings

        rt = await get_runtime_settings(session)
        rt.paper_entry_mode = ENTRY_MODE_BALANCED
        rt.balanced_entry_shadow_mode = False
        await session.commit()

        from tests.test_opportunity_monitor import _wallet_and_settings

        await _wallet_and_settings(session, auto=True)
        setup = AiProposedSetup(
            setup_id="pre-entry-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=WAIT_LONG,
            setup_mode="conditional",
            monitoring_enabled=True,
            monitor_from_ms=boundary,
            monitor_cursor_ms=bar_5m.time_ms - STEP5,
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
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
                "monitor_1m_cursor_ms": boundary - 1,
            },
            conditional_plan={"confirmation_sequence": ["liquidity_sweep", "mss", "entry_zone_touch"]},
            structured_snapshot={"confirmation_type": "sweep_mss_poi"},
        )
        session.add(setup)
        await session.commit()

        partial_1m = [c for c in closed_1m if boundary < c.time_ms <= m3]
        bundle_5m = KlineBundle(
            closed=closed_5m,
            forming=None,
            gaps_detected=[],
            gaps_remaining=[],
            data_complete=True,
            interval="5m",
        )
        bundle_1m = KlineBundle(
            closed=closed_1m,
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
                            with patch(
                                "app.services.opportunity_monitor._net_rr_ok",
                                AsyncMock(return_value=(True, 2.5)),
                            ):
                                summary = await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()

        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "pre-entry-1"))).scalar_one()
        policy = resolve_confirmation_policy(
            sequence=["liquidity_sweep", "mss", "entry_zone_touch"],
            structured=row.structured_snapshot or {},
        )
        from app.services.confirmation_policy import promotion_ready

        assert now_ms < candle_close_ms(bar_5m, "5m")
        assert promotion_ready(policy, row.confirmation_state or {}, allow_pre_entry=True)
        assert row.lifecycle_state in (READY_LONG, "PENDING_ENTRY")
        assert summary.get("orders", 0) >= 1
        ts = (row.confirmation_state or {}).get("confirmation_timestamps") or {}
        assert "liquidity_sweep" in ts and "mss" in ts
        assert "entry_zone_touch" not in ts


@pytest.mark.asyncio
async def test_shadow_does_not_mutate_live_confirmations_with_balanced_engine():
    await init_db()
    closed_5m, closed_1m, bar_5m, now_ms = _build_positive_same_5m()
    boundary = bar_5m.time_ms - 14 * STEP5

    async with SessionLocal() as session:
        from app.services.runtime_settings import get_runtime_settings

        rt = await get_runtime_settings(session)
        rt.paper_entry_mode = ENTRY_MODE_BALANCED
        rt.balanced_entry_shadow_mode = True
        await session.commit()

        from tests.test_opportunity_monitor import _wallet_and_settings

        await _wallet_and_settings(session, auto=False)
        setup = AiProposedSetup(
            setup_id="shadow-iso-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=WAIT_LONG,
            monitoring_enabled=True,
            monitor_from_ms=boundary,
            monitor_cursor_ms=bar_5m.time_ms - STEP5,
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
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
                "monitor_1m_cursor_ms": boundary - 1,
            },
            structured_snapshot={"confirmation_type": "sweep_mss_poi"},
        )
        session.add(setup)
        await session.commit()
        live_before = dict(setup.confirmation_state or {})

        bundle_5m = KlineBundle(closed=closed_5m, forming=None, gaps_detected=[], gaps_remaining=[], data_complete=True, interval="5m")
        bundle_1m = KlineBundle(closed=closed_1m, forming=None, gaps_detected=[], gaps_remaining=[], data_complete=True, interval="1m")
        mon = OpportunityMonitorService()

        async def _fetch(symbol: str, interval: str, limit: int):
            return bundle_1m if interval == "1m" else bundle_5m

        with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(side_effect=_fetch)):
            with patch("app.services.opportunity_monitor._utc_now") as mock_now:
                mock_now.return_value = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
                with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 30, 330)):
                    with patch(
                        "app.services.opportunity_monitor.setup_history_covers_cursor",
                        return_value=(True, "ok"),
                    ):
                        with patch(
                            "app.services.opportunity_monitor.detect_skipped_closed_bars",
                            return_value=[],
                        ):
                            with patch(
                                "app.services.opportunity_monitor._net_rr_ok",
                                AsyncMock(return_value=(True, 2.5)),
                            ):
                                await mon.monitor_setups(session, symbol="BTCUSDT")
        await session.commit()

        row = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == "shadow-iso-1"))).scalar_one()
        live_after = row.confirmation_state or {}
        shadow = (row.objective_notes or {}).get("balanced_shadow_sim") or {}
        assert (live_after.get("confirmation_engine") or "STRICT") != "BALANCED"
        assert shadow.get("would_promote_ready") is True
        assert len(live_after.get("confirmation_timestamps") or {}) <= len(live_before.get("confirmation_timestamps") or {}) + 1
