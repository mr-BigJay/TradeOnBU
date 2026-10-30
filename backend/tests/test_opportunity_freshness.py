from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import pytest
from app.domain.candles import Candle
from app.services.opportunity_freshness import seed_recent_structure, consume_unordered_opportunity, STEP
from app.services.conditional_revision import apply_conditional_revision_to_setup_row
from app.services.confirmation_policy import resolve_confirmation_policy, promotion_ready
from app.services.revision_replay import assess_revision_replay_trust

END = (int(datetime.now(timezone.utc).timestamp() * 1000) // STEP - 1) * STEP

def bar(t, o=100, h=101, l=99, c=100):
    return Candle(time_ms=t, open=o, high=h, low=l, close=c, volume=1)

def setup(direction="LONG", seq=None):
    seq = seq or ["liquidity_sweep", "mss", "entry_zone_touch"]
    return SimpleNamespace(
        setup_id="test", setup_mode="conditional", direction=direction, status="open", lifecycle_state="WAIT_" + direction,
        entry_zone_low=99.5, entry_zone_high=100.5, entry_price=100,
        stop_loss=95 if direction == "LONG" else 105,
        take_profit=110 if direction == "LONG" else 90, invalidation_level=95 if direction == "LONG" else 105,
        monitor_from_ms=END+STEP, monitor_cursor_ms=END+STEP-1,
        conditional_plan={"freshness_policy_version": 1, "registered_at_ms": END+1000},
        structured_snapshot={}, confirmation_state={"sequence": seq, "required": seq, "confirmation_timestamps": {}},
        objective_notes={}, monitoring_enabled=True, closed_at=None, last_validation_result={}, last_rejection_reason=None,
    )

def history():
    bars = [bar(t) for t in range(END-24*STEP, END, STEP)]
    bars[-4] = bar(END-4*STEP, l=98, c=100)
    bars[-3] = bar(END-3*STEP, o=100, h=103, l=100, c=102)
    bars[-2] = bar(END-2*STEP, o=102, h=104, l=101, c=103)
    bars[-1] = bar(END-STEP, o=103, h=104, l=102, c=103)
    return bars

def test_seed_prefix_only_and_balanced_can_prepare_limit_without_touch():
    row = setup()
    assert seed_recent_structure(row, history(), data_complete=True) is None
    assert row.confirmation_state["completed"] == ["liquidity_sweep", "mss"]
    assert row.monitor_from_ms == END+STEP
    policy = resolve_confirmation_policy(sequence=row.confirmation_state["sequence"])
    assert promotion_ready(policy, row.confirmation_state, allow_pre_entry=True)
    assert not promotion_ready(policy, row.confirmation_state, allow_pre_entry=False)
    status, _, seed, start = assess_revision_replay_trust(
        confirmation_state=row.confirmation_state, objective_notes=row.objective_notes, monitor_from_ms=row.monitor_from_ms)
    assert status == "ok" and seed["confirmation_timestamps"] == row.confirmation_state["confirmation_timestamps"]
    assert start == row.monitor_from_ms

@pytest.mark.parametrize("variant", ["gap", "incomplete", "future", "old"])
def test_no_unproven_seed(variant):
    row, bars = setup(), history()
    if variant == "gap":
        bars.pop(5)
    elif variant == "future":
        bars = [bar(b.time_ms) for b in bars] + [bar(END, l=98, c=100), bar(END+STEP, h=103, c=102)]
    elif variant == "old":
        bars = [bar(b.time_ms) for b in bars]
        bars[1] = bar(bars[1].time_ms, l=98, c=100)
        bars[2] = bar(bars[2].time_ms, h=103, c=102)
    seed_recent_structure(row, bars, data_complete=variant != "incomplete")
    assert not row.confirmation_state["confirmation_timestamps"]

@pytest.mark.parametrize("kind", ["touch", "target", "invalidation"])
def test_history_already_used_or_invalidated(kind):
    row, bars = setup(), history()
    if kind == "touch":
        bars[-1] = bar(END-STEP, o=103, h=104, l=100, c=103)
    elif kind == "target":
        bars[-1] = bar(END-STEP, o=103, h=111, l=102, c=110)
    else:
        bars[-1] = bar(END-STEP, o=103, h=103, l=93, c=94)
    result = seed_recent_structure(row, bars, data_complete=True)
    if kind == "invalidation":
        assert not row.confirmation_state["confirmation_timestamps"]
    else:
        assert result == ("entry_zone_used_before_registration" if kind == "touch" else "target_reached_before_registration")
        assert row.status == "expired" and not row.monitoring_enabled
    assert "entry_zone_touch" not in row.confirmation_state["confirmation_timestamps"]

def test_breakout_requires_later_forward_retest():
    row = setup(seq=["breakout_close", "breakout_retest", "entry_zone_touch"])
    row.structured_snapshot = {"breakout_level": 100.5}
    bars = [bar(t) for t in range(END-24*STEP, END, STEP)]
    bars[-1] = bar(END-STEP, h=103, l=100, c=102)
    seed_recent_structure(row, bars, data_complete=True)
    assert row.confirmation_state["completed"] == ["breakout_close"]

@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_target_without_touch_retires(direction):
    row = setup(direction)
    b = bar(END+STEP, o=108, h=111, l=107, c=110) if direction == "LONG" else bar(END+STEP, o=92, h=93, l=89, c=90)
    assert consume_unordered_opportunity(row, [b], interval_ms=STEP) == "target_reached_without_order"
    assert row.last_validation_result["reason"] == row.last_rejection_reason

@pytest.mark.parametrize("direction,close", [("LONG", 102), ("SHORT", 98)])
def test_first_touch_then_departure_retires_across_ticks(direction, close):
    row = setup(direction)
    assert consume_unordered_opportunity(row, [bar(END+STEP)], interval_ms=STEP) is None
    b = bar(END+2*STEP, o=100, h=103, l=97, c=close)
    assert consume_unordered_opportunity(row, [b], interval_ms=STEP) == "first_touch_missed"
    assert row.status == "expired"

def test_pre_boundary_touch_is_not_a_forward_fill_or_miss():
    row = setup()
    assert consume_unordered_opportunity(row, [bar(END, h=112, c=110)], interval_ms=STEP) is None
    assert row.status == "open"

def test_structural_revision_archives_old_touch_flags():
    row = setup()
    row.expires_at = datetime.now(timezone.utc) + timedelta(hours=3)
    row.entry_touched = row.tp_touched = True
    row.sl_touched = row.sequencing_ambiguous = row.paper_fill_recorded = False
    row.last_tracked_candle_ms = END
    row.objective_notes = {"first_touch_ms": {"entry": END}, "tp_pre_entry": True}
    plan = dict(potential_direction="LONG", confirmation_sequence=["breakout_close", "breakout_retest", "entry_zone_touch"],
                confirmation_type="level_breakout_retest", breakout_level=101, entry_zone_low=100, entry_zone_high=102,
                proposed_entry_mid=101, proposed_stop_loss=94, proposed_take_profit=120, structural_invalidation_level=94)
    impact = apply_conditional_revision_to_setup_row(row, plan=plan, run_id=2, structured={"breakout_level": 101},
                                                    revision_at=datetime.now(timezone.utc))
    assert impact.value == "structural"
    assert not row.entry_touched and not row.tp_touched
    assert "first_touch_ms" not in row.objective_notes
    assert row.objective_notes["price_revision_history"][0]["tracking"]["tp_touched"]
    assert row.last_tracked_candle_ms == 0 and row.last_validation_result == {}

@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["pending", "missed", "target", "rr_blocked"])
async def test_monitor_with_real_seed_and_forward_order_gates(session, outcome):
    from unittest.mock import AsyncMock, patch
    from sqlalchemy import select
    from app.models import AiProposedSetup, SimPosition
    from app.services.market_data import KlineBundle
    from app.services.opportunity_monitor import OpportunityMonitorService
    from app.services.runtime_settings import get_runtime_settings
    from tests.test_opportunity_monitor import _wallet_and_settings

    rt = await get_runtime_settings(session)
    rt.paper_entry_mode = "BALANCED"
    rt.balanced_entry_shadow_mode = False
    await _wallet_and_settings(session, auto=True)
    base = setup()
    registered = END + 1000
    boundary = END + 60_000
    row = AiProposedSetup(
        setup_id="fresh-monitor-" + outcome, run_id=1, symbol="BTCUSDT", direction="LONG",
        status="open", lifecycle_state="WAIT_LONG", setup_mode="conditional", monitoring_enabled=True,
        monitor_from_ms=boundary, monitor_cursor_ms=boundary-1,
        created_at=datetime.fromtimestamp(registered/1000, timezone.utc),
        expires_at=datetime.fromtimestamp((END+4*3600_000)/1000, timezone.utc),
        entry_zone_low=99.5, entry_zone_high=100.5, entry_price=100,
        stop_loss=95, take_profit=109 if outcome == "rr_blocked" else 120, invalidation_level=95,
        conditional_plan=base.conditional_plan, confirmation_state=base.confirmation_state,
        structured_snapshot={"confirmation_type": "sweep_mss_poi"}, objective_notes={},
    )
    session.add(row)
    await session.flush()
    one = [bar(t, o=103, h=104, l=102, c=103) for t in range(END, END+STEP, 60_000)]
    if outcome == "missed":
        one[-1] = bar(one[-1].time_ms, o=103, h=104, l=100, c=103)
    elif outcome == "target":
        one[-1] = bar(one[-1].time_ms, o=103, h=121, l=102, c=120)
    five = history() + [bar(END, o=103, h=max(b.high for b in one), l=min(b.low for b in one), c=one[-1].close)]
    def bundle(bars, interval):
        return KlineBundle(closed=bars, forming=None, gaps_detected=[], gaps_remaining=[], data_complete=True, interval=interval)
    async def fetch(symbol, interval, limit):
        return bundle(one, "1m") if interval == "1m" else bundle(five, "5m")
    mon = OpportunityMonitorService()
    now = datetime.fromtimestamp((END+STEP+2000)/1000, timezone.utc)
    with patch.object(mon.market, "fetch_kline_bundle", AsyncMock(side_effect=fetch)), patch("app.services.opportunity_monitor._utc_now", return_value=now):
        await mon.monitor_setups(session, symbol="BTCUSDT")
    positions = list((await session.execute(select(SimPosition))).scalars())
    if outcome == "pending":
        assert len(positions) == 1, row.last_validation_result
        assert positions[0].status == "pending_entry"
        assert positions[0].fill_price is None
        assert row.confirmation_state["completed"] == ["liquidity_sweep", "mss"]
    elif outcome == "rr_blocked":
        assert not positions
        assert row.last_rejection_reason == "ict_confirmed_blocked_by_net_rr"
    else:
        assert not positions
        assert row.status == "expired"
        assert row.last_rejection_reason == ("first_touch_missed" if outcome == "missed" else "target_reached_without_order")

@pytest.mark.asyncio
async def test_identical_report_does_not_extend_expiry(session):
    from app.services.ai_setup_memory import create_setup_from_structured
    from app.domain.time_utils import ensure_utc
    from tests.test_conditional_revision_lifecycle import _short_setup_structured
    row, _ = await create_setup_from_structured(session, run_id=501, symbol="BTCUSDT", structured=_short_setup_structured())
    assert row is not None
    assert ensure_utc(row.expires_at) <= datetime.now(timezone.utc) + timedelta(hours=4)
    row.expires_at = datetime.now(timezone.utc) + timedelta(minutes=20)
    deadline = row.expires_at
    registered = row.conditional_plan["registered_at_ms"]
    await session.flush()
    revised, meta = await create_setup_from_structured(session, run_id=502, symbol="BTCUSDT", structured=_short_setup_structured(trade_reason="new report, same plan"))
    assert meta["updated"] and revised.setup_id == row.setup_id
    assert ensure_utc(revised.expires_at) == deadline
    assert revised.conditional_plan["registered_at_ms"] == registered

@pytest.mark.parametrize("case,action", [
    ("target_first", "cancel"), ("fill_first", "fill"), ("same_bar", "suspend"),
    ("gap", "suspend"), ("creation_bar", "suspend"), ("no_target", "hold"),
])
def test_pending_target_vs_fill_chronology(case, action):
    from app.domain.execution_order_policy import reconcile_pending_ai_limit_order
    candles = [bar(END+i*60_000, o=105, h=106, l=104, c=105) for i in range(3)]
    if case != "no_target":
        candles[-1] = bar(candles[-1].time_ms, o=108, h=111, l=107, c=110)
    if case == "fill_first":
        candles[0] = bar(END, o=101, h=102, l=99, c=101)
    elif case == "same_bar":
        candles[-1] = bar(candles[-1].time_ms, o=105, h=111, l=99, c=110)
    elif case == "gap":
        candles.pop(1)
    elif case == "creation_bar":
        candles[0] = bar(END, o=105, h=111, l=104, c=110)
    result = reconcile_pending_ai_limit_order(
        order_ms=END+1000 if case == "creation_bar" else END, direction="LONG",
        invalidation_level=95, monitor_cursor_ms=END-STEP, limit_price=100,
        closed_1m=candles, closed_5m=[], now_ms=END+STEP, five_m_data_verified=True,
        one_m_data_complete=case != "gap", target_price=110)
    assert result.action == action
    if action == "cancel":
        assert result.reason == "target_reached_before_fill"


def test_price_revision_uses_new_tracking_boundary_and_archives_seed():
    from app.services.ai_setup_memory import _tracking_start_ms
    row = setup()
    row.created_at = datetime.fromtimestamp((END-5*STEP)/1000, timezone.utc)
    row.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    row.entry_touched = row.tp_touched = True
    row.sl_touched = row.sequencing_ambiguous = row.paper_fill_recorded = False
    row.last_tracked_candle_ms = END
    seed_recent_structure(row, history(), data_complete=True)
    revised_at = datetime.fromtimestamp((END+STEP+1000)/1000, timezone.utc)
    plan = dict(potential_direction="LONG", confirmation_sequence=["liquidity_sweep", "mss", "entry_zone_touch"],
                entry_zone_low=99.6, entry_zone_high=100.6, proposed_entry_mid=100.1,
                proposed_stop_loss=95, proposed_take_profit=110, structural_invalidation_level=95)
    apply_conditional_revision_to_setup_row(row, plan=plan, run_id=2, structured={}, revision_at=revised_at)
    assert not row.entry_touched and not row.tp_touched
    assert "historical_confirmation_seed" not in row.objective_notes
    assert _tracking_start_ms(row) == END+2*STEP

@pytest.mark.parametrize("fills", [False, True])
def test_pending_zone_reaction_does_not_wait_for_second_touch(fills):
    from app.domain.execution_order_policy import reconcile_pending_ai_limit_order
    candles = [bar(END, o=102, h=103, l=99.9 if fills else 100.4, c=102)]
    result = reconcile_pending_ai_limit_order(
        order_ms=END, direction="LONG", invalidation_level=95, monitor_cursor_ms=END-STEP,
        limit_price=100, closed_1m=candles, closed_5m=[], now_ms=END+STEP,
        five_m_data_verified=True, target_price=110, entry_zone=(99.5, 100.5))
    assert result.action == ("fill" if fills else "cancel")
    if not fills:
        assert result.reason == "first_zone_reaction_before_fill"
