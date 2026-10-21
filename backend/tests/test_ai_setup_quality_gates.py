"""Quality gates: regime, SL status, structural repeat evidence, revisions, pre-order checks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.domain.candles import Candle
from app.domain.setup_lifecycle import READY_SHORT, WAIT_LONG, WAIT_SHORT
from app.models import AiProposedSetup, SimPosition
from app.services.ai_setup_memory import create_setup_from_structured
from app.domain.candle_utils import candle_close_ms, interval_ms

from app.services.ai_setup_quality_gates import (
    _active_sim_same_direction_excluding_setup,
    _recent_losing_sim_same_direction,
    confirmations_strictly_after_ms,
    is_sim_summary_loss,
    objective_sequence_evidence_after_ms,
    sim_position_failure_ms,
    validate_ready_setup_before_paper_order,
    validate_setup_quality_at_execution,
    validate_setup_quality_gates,
)
from app.services.market_data import KlineBundle
from tests.test_ai_trader_persistence_contract import (
    _production_conditional_wait_long,
    _production_immediate_long_monitored,
)


def _closed_5m_bar(*, now_ms: int | None = None, close: float = 84000.0) -> tuple[Candle, int]:
    now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    step = interval_ms("5m")
    open_ms = (now_ms // step) * step - step
    bar = Candle(time_ms=open_ms, open=close, high=close + 50, low=close - 50, close=close, volume=1.0)
    assert now_ms >= candle_close_ms(bar, "5m")
    return bar, now_ms


def _loss_position(*, public_id: str, status: str = "sl_hit") -> SimPosition:
    return SimPosition(
        public_id=public_id,
        symbol="BTCUSDT",
        direction="SHORT",
        status=status,
        margin_usdt=50.0,
        leverage=10,
        entry_price=84000.0,
        stop_loss=84500.0,
        take_profit=82000.0,
        fill_price=84000.0,
        exit_price=84500.0,
        net_pnl_usdt=-15.0,
        close_reason="stop_loss" if status == "sl_hit" else "closed",
        closed_at=datetime.now(timezone.utc) - timedelta(hours=3),
        updated_at=datetime.now(timezone.utc) - timedelta(hours=3),
    )


def _conditional_short_against_bullish(**extra) -> dict:
    base = {
        "decision": "NO_TRADE",
        "final_ai_decision": "WAIT",
        "setup_mode": "conditional",
        "potential_direction": "SHORT",
        "market_regime": "bullish",
        "trade_against_dominant_trend": True,
        "conditional_entry_zone_low": 84400.0,
        "conditional_entry_zone_high": 84600.0,
        "confirmation_required": "نیاز به sweep نزولی و MSS نزولی",
        "confirmation_type": "sweep_mss_poi",
        "structural_invalidation_level": 85200.0,
        "conditional_invalidation": "شکست سطح ابطال ساختاری",
        "stop_loss": 85200.0,
        "take_profit": 82000.0,
        "risk_reward": 2.2,
        "liquidity_event_clear": False,
        "poi_reaction_clear": False,
        "structural_confirmation_clear": False,
        "logical_invalidation_clear": True,
        "proposed_setup": {"new_setup": True},
    }
    base.update(extra)
    return base


@pytest.mark.parametrize("public_id", ["paper-pos-8", "paper-pos-9", "paper-pos-10"])
def test_is_sim_summary_loss_treats_sl_hit_like_production(public_id: str):
    row = {
        "sim_position_id": public_id,
        "status": "sl_hit",
        "net_pnl_usdt": -10.0,
        "close_reason": "stop_loss",
    }
    assert is_sim_summary_loss(row) is True


@pytest.mark.asyncio
async def test_recent_loss_query_includes_sl_hit_not_only_closed():
    await init_db()
    async with SessionLocal() as session:
        session.add(_loss_position(public_id="paper-pos-10", status="sl_hit"))
        await session.commit()
        loss = await _recent_losing_sim_same_direction(session, symbol="BTCUSDT", direction="SHORT")
        assert loss is not None
        assert loss.public_id == "paper-pos-10"
        assert loss.status == "sl_hit"


@pytest.mark.asyncio
async def test_conditional_wait_short_in_bullish_allowed_without_upfront_ict_booleans():
    await init_db()
    async with SessionLocal() as session:
        session.add(_loss_position(public_id="paper-pos-9"))
        await session.commit()
    data = _conditional_short_against_bullish()
    async with SessionLocal() as session:
        err, audit = await validate_setup_quality_gates(session, symbol="BTCUSDT", structured=data)
        assert err is None
        assert any(c.get("deferred_to_monitor") for c in audit.get("checks", []) if c.get("gate") == "repeat_direction_after_loss")
        row, meta = await create_setup_from_structured(session, run_id=1, symbol="BTCUSDT", structured=data)
        await session.commit()
        assert meta.get("created") is True
        assert row.lifecycle_state == WAIT_SHORT


@pytest.mark.asyncio
async def test_immediate_short_after_sl_hit_requires_objective_structure_on_closed_5m():
    await init_db()
    async with SessionLocal() as session:
        session.add(_loss_position(public_id="paper-pos-8"))
        await session.commit()
    data = _production_immediate_long_monitored()
    data.update(
        {
            "decision": "SHORT",
            "final_ai_decision": "SHORT",
            "entry_price": 84000.0,
            "entry_zone_low": 83900.0,
            "entry_zone_high": 84100.0,
            "stop_loss": 84500.0,
            "take_profit": 82000.0,
            "structural_invalidation_level": 85200.0,
        }
    )
    empty = KlineBundle(
        closed=[Candle(time_ms=1_700_000_000_000, open=100, high=100.1, low=99.9, close=100, volume=1.0)],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )
    with patch("app.services.market_data.MarketDataService") as mcls:
        mcls.return_value.fetch_kline_bundle = AsyncMock(return_value=empty)
        async with SessionLocal() as session:
            err, _ = await validate_setup_quality_gates(session, symbol="BTCUSDT", structured=data)
            assert err == "repeat_direction_no_objective_structure_since_loss"


@pytest.mark.asyncio
async def test_conditional_revision_runs_quality_gates_and_records_audit():
    await init_db()
    data = _production_conditional_wait_long()
    async with SessionLocal() as session:
        row1, _ = await create_setup_from_structured(session, run_id=10, symbol="BTCUSDT", structured=data)
        await session.commit()
        setup_id = row1.setup_id
        data2 = dict(data)
        data2["confirmation_required"] = "بازبینی با کنترل کیفیت"
        row2, meta = await create_setup_from_structured(session, run_id=11, symbol="BTCUSDT", structured=data2)
        await session.commit()
        assert meta.get("reason") == "conditional_revision"
        assert row2.setup_id == setup_id
        notes = row2.objective_notes or {}
        assert notes.get("setup_quality_audit")
        assert notes.get("quality_audit_history")


@pytest.mark.asyncio
async def test_conditional_wait_keeps_false_initial_flags():
    await init_db()
    data = _production_conditional_wait_long()
    async with SessionLocal() as session:
        row, meta = await create_setup_from_structured(session, run_id=3, symbol="BTCUSDT", structured=data)
        await session.commit()
        assert meta.get("created") is True
        assert row.lifecycle_state == WAIT_LONG
        assert (row.structured_snapshot or {}).get("poi_reaction_clear") is False


@pytest.mark.asyncio
async def test_pre_order_rejects_confirmations_stamped_before_prior_sl_hit():
    await init_db()
    async with SessionLocal() as session:
        loss_row = _loss_position(public_id="paper-pos-8")
        session.add(loss_row)
        await session.flush()
        failure_ms = sim_position_failure_ms(loss_row)
        setup = AiProposedSetup(
            setup_id="pre-q-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="SHORT",
            status="open",
            lifecycle_state=READY_SHORT,
            entry_price=84000.0,
            stop_loss=84500.0,
            take_profit=82000.0,
            invalidation_level=85200.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            confirmation_state={
                "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "confirmation_timestamps": {
                    "liquidity_sweep": failure_ms - 600_000,
                    "mss": failure_ms - 300_000,
                    "entry_zone_touch": failure_ms - 60_000,
                },
            },
        )
        session.add(setup)
        await session.commit()
        bar, now_ms = _closed_5m_bar()
        ok, reason = await validate_ready_setup_before_paper_order(
            session,
            setup,
            latest_closed_5m=bar,
            now_ms=now_ms,
            market_data_verified=True,
        )
        assert ok is False
        assert reason == "confirmations_not_after_prior_same_direction_loss"


@pytest.mark.asyncio
async def test_pre_order_blocks_stacked_same_direction_sim_position():
    await init_db()
    async with SessionLocal() as session:
        session.add(
            SimPosition(
                public_id="other-short-open",
                symbol="BTCUSDT",
                direction="SHORT",
                status="open",
                margin_usdt=50.0,
                leverage=10,
                entry_price=84100.0,
                stop_loss=84600.0,
                take_profit=82100.0,
                ai_setup_id="other-setup",
            )
        )
        setup = AiProposedSetup(
            setup_id="pre-q-2",
            run_id=2,
            symbol="BTCUSDT",
            direction="SHORT",
            status="open",
            lifecycle_state=READY_SHORT,
            entry_price=84000.0,
            stop_loss=84500.0,
            take_profit=82000.0,
            invalidation_level=85200.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            confirmation_state={
                "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "confirmation_timestamps": {
                    "liquidity_sweep": 1_700_000_000_000,
                    "mss": 1_700_000_300_000,
                    "entry_zone_touch": 1_700_000_600_000,
                },
            },
        )
        session.add(setup)
        await session.commit()
        bar, now_ms = _closed_5m_bar()
        ok, reason = await validate_ready_setup_before_paper_order(
            session,
            setup,
            latest_closed_5m=bar,
            now_ms=now_ms,
            market_data_verified=True,
        )
        assert ok is False
        assert reason == "stacked_same_direction_paper_position"


def test_confirmations_strictly_after_ms_requires_increasing_after_failure():
    setup = AiProposedSetup(
        setup_id="x",
        run_id=1,
        symbol="BTCUSDT",
        direction="SHORT",
        status="open",
        confirmation_state={
            "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
            "confirmation_timestamps": {
                "liquidity_sweep": 1000,
                "mss": 2000,
                "entry_zone_touch": 3000,
            },
        },
    )
    assert confirmations_strictly_after_ms(setup, 500)[0] is True
    assert confirmations_strictly_after_ms(setup, 2500)[0] is False


@pytest.mark.asyncio
async def test_pre_order_fail_closed_when_market_data_unverified():
    await init_db()
    async with SessionLocal() as session:
        setup = AiProposedSetup(
            setup_id="pre-q-unverified",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=READY_SHORT,
            entry_price=84000.0,
            stop_loss=83500.0,
            take_profit=86000.0,
            invalidation_level=83000.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            confirmation_state={"sequence": [], "required": [], "confirmation_timestamps": {}},
        )
        session.add(setup)
        await session.commit()
        bar, now_ms = _closed_5m_bar()
        ok, reason = await validate_ready_setup_before_paper_order(
            session,
            setup,
            latest_closed_5m=bar,
            now_ms=now_ms,
            market_data_verified=False,
        )
        assert ok is False
        assert reason == "market_data_unverified_for_order"


@pytest.mark.asyncio
async def test_stacked_same_direction_includes_manual_position_with_null_ai_setup_id():
    await init_db()
    async with SessionLocal() as session:
        session.add(
            SimPosition(
                public_id="manual-short-open",
                symbol="BTCUSDT",
                direction="SHORT",
                status="open",
                margin_usdt=50.0,
                leverage=10,
                entry_price=84100.0,
                stop_loss=84600.0,
                take_profit=82100.0,
                ai_setup_id=None,
            )
        )
        setup = AiProposedSetup(
            setup_id="auto-short-setup",
            run_id=2,
            symbol="BTCUSDT",
            direction="SHORT",
            status="open",
            lifecycle_state=READY_SHORT,
            entry_price=84000.0,
            stop_loss=84500.0,
            take_profit=82000.0,
            invalidation_level=85200.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            confirmation_state={
                "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "confirmation_timestamps": {
                    "liquidity_sweep": 1_700_000_000_000,
                    "mss": 1_700_000_300_000,
                    "entry_zone_touch": 1_700_000_600_000,
                },
            },
        )
        session.add(setup)
        await session.commit()
        other = await _active_sim_same_direction_excluding_setup(
            session,
            symbol="BTCUSDT",
            direction="SHORT",
            setup_id=setup.setup_id,
        )
        assert other is not None
        assert other.public_id == "manual-short-open"


@pytest.mark.asyncio
async def test_fill_revalidation_rejects_stale_pending_after_structural_invalidation():
    await init_db()
    async with SessionLocal() as session:
        setup = AiProposedSetup(
            setup_id="fill-reval-1",
            run_id=3,
            symbol="BTCUSDT",
            direction="SHORT",
            status="open",
            lifecycle_state="PENDING_ENTRY",
            entry_price=84000.0,
            stop_loss=84500.0,
            take_profit=82000.0,
            invalidation_level=85200.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            confirmation_state={
                "sequence": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "required": ["liquidity_sweep", "mss", "entry_zone_touch"],
                "confirmation_timestamps": {
                    "liquidity_sweep": 1_700_000_000_000,
                    "mss": 1_700_000_300_000,
                    "entry_zone_touch": 1_700_000_600_000,
                },
            },
        )
        session.add(setup)
        await session.commit()
        bar, now_ms = _closed_5m_bar(close=85300.0)
        from app.domain.candle_utils import candle_close_ms

        ok, reason = await validate_setup_quality_at_execution(
            session,
            setup,
            latest_closed_5m=None,
            market_data_verified=True,
            now_ms=now_ms,
            phase="fill",
            exclude_sim_public_id="pending-sim-1",
            closed_5m=[bar],
            as_of_ms=candle_close_ms(bar, "5m"),
        )
        assert ok is False
        assert reason == "structural_invalidation_level_breached"


def _fvg_structured_bos() -> dict:
    return {
        "chart_overlays": {
            "fvg_zones": [{"low": 99.5, "high": 100.5, "direction": "bullish"}],
        },
        "conditional_entry_zone_low": 99.0,
        "conditional_entry_zone_high": 101.0,
    }


@pytest.mark.parametrize(
    "confirmation_type,sequence",
    [
        ("bos_fvg_retest", ["bos", "fvg_retest", "entry_zone_touch"]),
        ("mss_fvg_retest", ["mss", "fvg_retest", "entry_zone_touch"]),
    ],
)
def test_fvg_retest_evidence_positive_with_structured_zones(confirmation_type, sequence):
    structured = _fvg_structured_bos()
    t0 = 1_700_000_000_000
    bars: list[Candle] = []
    for i in range(14):
        bars.append(
            Candle(
                time_ms=t0 + i * 300_000,
                open=100.0,
                high=100.5,
                low=99.5,
                close=100.0,
                volume=1.0,
            )
        )
    bars.append(
        Candle(time_ms=t0 + 14 * 300_000, open=100.0, high=101.2, low=99.9, close=101.0, volume=1.0)
    )
    bars.append(
        Candle(time_ms=t0 + 15 * 300_000, open=100.2, high=100.6, low=99.4, close=100.0, volume=1.0)
    )
    bars.append(
        Candle(time_ms=t0 + 16 * 300_000, open=100.0, high=101.0, low=99.8, close=100.5, volume=1.0)
    )
    ok, hits = objective_sequence_evidence_after_ms(
        direction="LONG",
        sequence=sequence,
        closed_5m=bars,
        after_ms=t0 - 1,
        zone_lo=99.0,
        zone_hi=101.0,
        structured=structured,
    )
    assert "fvg_retest" in hits
    assert hits["fvg_retest"] >= t0
    assert ok is True


@pytest.mark.parametrize(
    "confirmation_type,sequence",
    [
        ("bos_fvg_retest", ["bos", "fvg_retest", "entry_zone_touch"]),
        ("mss_fvg_retest", ["mss", "fvg_retest", "entry_zone_touch"]),
    ],
)
def test_fvg_retest_evidence_negative_without_fvg_zones(confirmation_type, sequence):
    t0 = 1_700_000_000_000
    bars = [
        Candle(time_ms=t0 + 300_000, open=100.0, high=100.2, low=99.4, close=100.0, volume=1),
        Candle(time_ms=t0 + 600_000, open=100.0, high=100.3, low=99.6, close=100.1, volume=1),
        Candle(time_ms=t0 + 900_000, open=100.0, high=101.0, low=99.8, close=100.5, volume=1),
    ]
    ok, hits = objective_sequence_evidence_after_ms(
        direction="LONG",
        sequence=sequence,
        closed_5m=bars,
        after_ms=t0 - 1,
        zone_lo=99.0,
        zone_hi=101.0,
        structured={},
    )
    assert "fvg_retest" not in hits
    assert ok is False


def test_objective_sequence_evidence_enforces_chronological_token_order_positive():
    """Each ICT token must appear in sequence order on strictly later bars."""
    t0 = 1_000_000
    bars = [
        Candle(time_ms=t0 + 300_000, open=100, high=100, low=100, close=100, volume=1),
        Candle(time_ms=t0 + 600_000, open=100, high=100, low=100, close=100, volume=1),
        Candle(time_ms=t0 + 900_000, open=100, high=100, low=100, close=100, volume=1),
    ]
    sequence = ["liquidity_sweep", "mss", "entry_zone_touch"]

    def _tok(tok: str, *, window, **kwargs):
        ms = window[-1].time_ms
        return (tok, ms) in {
            ("liquidity_sweep", t0 + 300_000),
            ("mss", t0 + 600_000),
            ("entry_zone_touch", t0 + 900_000),
        }

    with patch("app.services.ai_setup_quality_gates._token_on_bar", side_effect=_tok):
        ok, hits = objective_sequence_evidence_after_ms(
            direction="LONG",
            sequence=sequence,
            closed_5m=bars,
            after_ms=t0 - 1,
            zone_lo=99.0,
            zone_hi=101.0,
        )
    assert ok is True
    assert hits == {
        "liquidity_sweep": t0 + 300_000,
        "mss": t0 + 600_000,
        "entry_zone_touch": t0 + 900_000,
    }


def test_objective_sequence_evidence_rejects_entry_zone_before_prior_tokens():
    """Early entry_zone_touch must not validate when sweep/mss come later."""
    t0 = 2_000_000
    bars = [
        Candle(time_ms=t0 + 300_000, open=100, high=100, low=100, close=100, volume=1),
        Candle(time_ms=t0 + 600_000, open=100, high=100, low=100, close=100, volume=1),
        Candle(time_ms=t0 + 900_000, open=100, high=100, low=100, close=100, volume=1),
    ]
    sequence = ["liquidity_sweep", "mss", "entry_zone_touch"]

    def _tok(tok: str, *, window, **kwargs):
        ms = window[-1].time_ms
        return (tok, ms) in {
            ("entry_zone_touch", t0 + 300_000),
            ("liquidity_sweep", t0 + 600_000),
            ("mss", t0 + 900_000),
        }

    with patch("app.services.ai_setup_quality_gates._token_on_bar", side_effect=_tok):
        ok, hits = objective_sequence_evidence_after_ms(
            direction="LONG",
            sequence=sequence,
            closed_5m=bars,
            after_ms=t0 - 1,
            zone_lo=99.0,
            zone_hi=101.0,
        )
    assert ok is False
    assert "entry_zone_touch" not in hits
    assert hits.get("liquidity_sweep") == t0 + 600_000
    assert hits.get("mss") == t0 + 900_000


@pytest.mark.asyncio
async def test_fill_quality_rejects_when_no_closed_5m_as_of_fill_event():
    await init_db()
    async with SessionLocal() as session:
        setup = AiProposedSetup(
            setup_id="fill-no-5m-asof",
            run_id=4,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state="PENDING_ENTRY",
            entry_price=100.0,
            stop_loss=94.0,
            take_profit=115.0,
            invalidation_level=93.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            confirmation_state={"sequence": [], "required": [], "confirmation_timestamps": {}},
        )
        session.add(setup)
        await session.commit()
        order_ms = 1_700_000_000_000
        fill_as_of = order_ms + 120_000
        future_5m = Candle(
            time_ms=order_ms + 300_000,
            open=100,
            high=101,
            low=99,
            close=100.5,
            volume=1.0,
        )
        ok, reason = await validate_setup_quality_at_execution(
            session,
            setup,
            latest_closed_5m=None,
            market_data_verified=True,
            now_ms=fill_as_of + 60_000,
            phase="fill",
            closed_5m=[future_5m],
            as_of_ms=fill_as_of,
        )
        assert ok is False
        assert reason == "missing_closed_5m_as_of_for_fill"


@pytest.mark.asyncio
async def test_sim_suspends_limit_fill_when_no_5m_as_of_fill_event():
    """Verified 5m bundle present but no bar closed yet at 1m fill time → suspend, not fill."""
    from unittest.mock import patch

    from app.domain.candle_utils import candle_close_ms
    from app.models import SimPositionSettings
    from app.services.market_data import KlineBundle
    from app.services.position.position_monitor import SimPositionMonitorService

    await init_db()
    order_ms = 1_700_000_100_000
    fill_bar = Candle(
        time_ms=order_ms + 60_000,
        open=99.5,
        high=100.5,
        low=99.0,
        close=100.0,
        volume=1.0,
    )
    fill_as_of = candle_close_ms(fill_bar, "1m")
    future_5m = Candle(
        time_ms=order_ms + 300_000,
        open=100,
        high=101,
        low=99,
        close=100.2,
        volume=1.0,
    )

    async with SessionLocal() as session:
        session.add(SimPositionSettings(id=1, auto_open_paper_positions=True, initial_balance_set=True))
        setup = AiProposedSetup(
            setup_id="suspend-no-5m-asof",
            run_id=5,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state="PENDING_ENTRY",
            monitoring_enabled=True,
            monitor_from_ms=order_ms,
            monitor_cursor_ms=order_ms - 300_000,
            entry_price=100.0,
            stop_loss=94.0,
            take_profit=115.0,
            invalidation_level=93.0,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        )
        session.add(setup)
        session.add(
            SimPosition(
                public_id="pos-suspend-asof",
                symbol="BTCUSDT",
                direction="LONG",
                status="pending_entry",
                order_type="limit",
                margin_usdt=50.0,
                leverage=10,
                limit_entry_price=100.0,
                stop_loss=94.0,
                take_profit=115.0,
                ai_setup_id="suspend-no-5m-asof",
                monitor_from_ms=order_ms,
                last_processed_candle_ms=0,
                extra={"auto_paper": True},
            )
        )
        await session.commit()

    bundle_1m = KlineBundle(
        closed=[fill_bar],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="1m",
    )
    bundle_5m = KlineBundle(
        closed=[future_5m],
        forming=None,
        gaps_detected=[],
        gaps_remaining=[],
        data_complete=True,
        interval="5m",
    )

    async def _fetch(symbol: str, interval: str, limit: int):
        return bundle_5m if interval == "5m" else bundle_1m

    sim = SimPositionMonitorService()
    async with SessionLocal() as session:
        with patch.object(sim.market, "fetch_kline_bundle", AsyncMock(side_effect=_fetch)):
            with patch(
                "app.services.opportunity_monitor._utc_now",
                return_value=datetime.fromtimestamp((fill_as_of + 30_000) / 1000, tz=timezone.utc),
            ):
                await sim.monitor_session(session)
        await session.commit()
        pos = (await session.execute(select(SimPosition))).scalar_one()
        setup = (await session.execute(select(AiProposedSetup))).scalar_one()
        assert pos.status == "pending_entry"
        notes = setup.objective_notes or {}
        assert notes.get("pending_fill_suspended") is True
        assert notes.get("pending_fill_block_reason") == "missing_closed_5m_as_of_for_fill"


def test_objective_sequence_evidence_false_when_tokens_missing_on_bars():
    t0 = 1_000_000
    bars = [
        Candle(time_ms=t0, open=100, high=100.2, low=99.8, close=100, volume=1),
    ]
    ok, hits = objective_sequence_evidence_after_ms(
        direction="LONG",
        sequence=["liquidity_sweep", "mss", "entry_zone_touch"],
        closed_5m=bars,
        after_ms=t0 - 1,
        zone_lo=99.0,
        zone_hi=101.0,
    )
    assert ok is False
    assert len(hits) < 3
