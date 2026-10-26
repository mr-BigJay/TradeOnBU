import pytest

from app.services.entry_strategies import A, B, C, save_strategy_settings
from app.services.strategy_candidates import persist_strategy_candidates
from app.services.ai_setup_memory import create_setup_from_structured
from app.services.strategy_features import closed_bar_features
from tests.test_entry_strategies import plan, bar
from tests.test_ai_trader_persistence_contract import _production_conditional_wait_long


@pytest.mark.asyncio
async def test_all_three_same_zone_persist_and_revise_independently(session):
    data = {**_production_conditional_wait_long(), "strategy_id": A,
            "strategy_candidates": {B: plan(B), C: plan(C)}}
    rows, results = await persist_strategy_candidates(session, run_id=1, symbol="BTCUSDT", structured=data)
    assert set(rows) == {A, B, C}, results
    assert all(r["created"] for r in results.values())
    assert len({r.setup_id for r in rows.values()}) == 3
    again, results = await persist_strategy_candidates(session, run_id=2, symbol="BTCUSDT", structured=data)
    assert all(r["updated"] for r in results.values()), results
    assert {s: r.setup_id for s, r in again.items()} == {s: r.setup_id for s, r in rows.items()}
    assert all("strategy_candidates" not in r.structured_snapshot for r in rows.values())


@pytest.mark.asyncio
async def test_invalid_sibling_does_not_block_others(session):
    data = {**plan(B), "strategy_candidates": {A: None, C: {**plan(C), "take_profit": 102}}}
    rows, results = await persist_strategy_candidates(session, run_id=1, symbol="BTCUSDT", structured=data)
    assert set(rows) == {B}
    assert results[C]["reason"] == "strategy_rr_below_two"
    assert results[A]["reason"] == "no_candidate"


@pytest.mark.asyncio
async def test_disabled_and_wrong_identity_fail_closed(session):
    await save_strategy_settings(session, {A: True, B: False, C: True})
    data = {**_production_conditional_wait_long(), "strategy_id": A,
            "strategy_candidates": {B: plan(B), C: plan(B)}}
    rows, results = await persist_strategy_candidates(session, run_id=1, symbol="BTCUSDT", structured=data)
    assert set(rows) == {A}
    assert results[B]["reason"] == "entry_strategy_disabled"
    assert results[C]["reason"] == "candidate_strategy_mismatch"


@pytest.mark.asyncio
async def test_same_strategy_different_zone_still_blocked(session):
    await create_setup_from_structured(session, run_id=1, symbol="BTCUSDT", structured=plan(B))
    data = {**plan(B), "conditional_entry_zone_low": 199, "conditional_entry_zone_high": 201,
            "stop_loss": 194, "structural_invalidation_level": 194, "take_profit": 225}
    row, meta = await create_setup_from_structured(session, run_id=2, symbol="BTCUSDT", structured=data)
    assert row is None and meta["reason"] == "parallel_same_direction_use_revision"


def test_feature_math_and_missing_history():
    bars = [bar(i * 300000, 100+i, 102+i, 99+i, 101+i) for i in range(40)]
    f = closed_bar_features(bars)
    assert f["atr14"] == 3
    assert f["adx14"] == 100
    assert f["relative_volume20"] == 1
    assert f["ema20_change"] == pytest.approx(1)
    assert f["body_atr"] == pytest.approx(1/3)
    assert not closed_bar_features(bars[:10])["available"]
    flat = [bar(i,100,100,100,100) for i in range(40)]
    assert closed_bar_features(flat)["body_atr"] is None


@pytest.mark.asyncio
async def test_siblings_still_share_execution_limit(session):
    from datetime import datetime, timezone
    from app.models import SimPosition
    from app.services.ai_setup_quality_gates import validate_setup_quality_at_execution
    data = {**plan(B), "strategy_candidates": {C: plan(C), A: None}}
    rows, _ = await persist_strategy_candidates(session, run_id=1, symbol="BTCUSDT", structured=data)
    session.add(SimPosition(public_id="existing-b", symbol="BTCUSDT", direction="LONG", status="pending_entry",
                            ai_setup_id=rows[B].setup_id, margin_usdt=50, leverage=10,
                            entry_price=100, stop_loss=94, take_profit=125, extra={"strategy_id": B}))
    await session.flush()
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    candle = bar(now // 300000 * 300000 - 300000, 100, 102, 99, 101)
    ok, reason = await validate_setup_quality_at_execution(session, rows[C], latest_closed_5m=candle,
                                                          market_data_verified=True, now_ms=now)
    assert not ok and reason == "stacked_same_direction_paper_position"


@pytest.mark.asyncio
async def test_run_persists_primary_and_sibling_audits(session, monkeypatch):
    from app.services import ai_ict_analysis as ai
    from tests.conftest import TestSession
    monkeypatch.setattr(ai, "SessionLocal", TestSession)
    run_id = await ai._create_run_record(trigger="test")
    draft = ai._RunDraft()
    draft.status = "completed"
    draft.decision = "NO_TRADE"
    draft.structured = {**plan(B), "strategy_candidates": {A: None, C: plan(C)}}
    run = await ai._persist_run_draft(run_id, draft, symbol="BTCUSDT")
    audit = run.structured["_artifacts"]
    assert audit["setup_persistence"]["created"]
    assert audit["strategy_persistence"][C]["created"]
    assert audit["opportunity"]["setup_id"] == audit["strategy_persistence"][B]["setup_id"]


def test_hindsight_review_never_mutates_candidate_or_supplies_entry_proof():
    from copy import deepcopy
    from app.services.strategy_timing_review import review_recent_patterns
    data = plan(B)
    original = deepcopy(data)
    bars = [bar(i * 300000, 103, 104, 102, 103) for i in range(9)]
    bars += [bar(9*300000, 102, 102, 99, 100), bar(10*300000, 100, 104, 100, 103), bar(11*300000, 103, 104, 100, 102)]
    review = review_recent_patterns(data, bars)
    assert review["mode"] == "hindsight_only_not_execution_evidence"
    assert review["strategies"][B]["completed"] == ["trend_pullback", "trend_resume", "entry_zone_touch"]
    assert data == original and "confirmation_state" not in data
    assert not review_recent_patterns(data, bars[1:])["strategies"][B]["available"]


def test_audit_uses_real_schema_and_does_not_attribute_unknown_orders():
    from pathlib import Path
    import runpy
    from sqlalchemy import create_engine, insert
    from app.db import Base
    from app.models import SimPosition
    audit = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/strategy_audit.py"))["audit"]
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(insert(SimPosition), [
            dict(public_id="known", symbol="BTCUSDT", direction="LONG", status="tp_hit",
                 margin_usdt=50, leverage=10, stop_loss=94, take_profit=125, net_pnl_usdt=12.5, extra={"strategy_id": B}),
            dict(public_id="unknown", symbol="BTCUSDT", direction="LONG", status="open",
                 margin_usdt=50, leverage=10, stop_loss=94, take_profit=125, net_pnl_usdt=None, extra={}),
        ])
        result = audit(connection.connection.driver_connection, 7)["strategies"]
    assert result[B]["net_pnl_usdt"] == 12.5 and result[B]["wins"] == 1
    assert result["legacy_unspecified"]["order_state:open"] == 1
    assert "losses" not in result["legacy_unspecified"]
    engine.dispose()
