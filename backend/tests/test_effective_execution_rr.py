"""Regression: paper net R:R must use capped executable TP and both-side fees."""

from __future__ import annotations

import pytest

from app.models import SimPositionSettings
from app.services.execution_model import ExecutionModel, ExitOutcome
from app.services.execution_model_sim import auto_paper_rr_from_settings
from app.services.position.calculations import compute_metrics
from app.services.position.fee_config import default_vip1_profile


def _sim():
    return SimPositionSettings(
        id=1, default_margin_usdt=50.0, default_leverage=10,
        maker_fee_rate=0.0002, taker_fee_rate=0.0005,
        slippage_bps=5.0, default_tp_behavior="limit",
    )


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_stop_exit_slippage_is_adverse_for_both_directions(direction):
    model = ExecutionModel()
    sl = model.close_fill(100.0, direction, ExitOutcome.SL)
    tp = model.close_fill(100.0, direction, ExitOutcome.TP)
    if direction == "LONG":
        assert sl < tp
    else:
        assert sl > tp


@pytest.mark.parametrize("direction,sl,tp", [
    ("LONG", 99.8, 101.0),
    ("SHORT", 100.2, 99.0),
])
def test_tight_sl_must_fail_after_execution_tp_is_capped(direction, sl, tp):
    rr = auto_paper_rr_from_settings(
        _sim(), direction=direction, entry=100.0, sl=sl, tp=tp, min_net_rr=2.0,
    )
    assert rr.gross_rr == pytest.approx(2.5)
    assert rr.net_rr < 2.0
    assert not rr.acceptable


def test_actual_entry_fill_is_not_slipped_twice():
    sim = _sim()
    rr = auto_paper_rr_from_settings(
        sim, direction="LONG", entry=99.95, sl=95, tp=110,
        entry_fill_price=99.95, min_net_rr=2.0,
    )
    assert rr.entry_fill == pytest.approx(99.95)


def test_reported_risk_includes_fees_and_reward_deducts_entry_fee():
    fees = default_vip1_profile()
    m = compute_metrics(
        direction="LONG", margin_usdt=50, leverage=10,
        entry_price=100, mark_price=100, stop_loss=95, take_profit=110,
        fees=fees, entry_is_maker=True, exit_is_maker=True,
    )
    qty = 5.0
    loss = qty * (100 - 95) + qty * 100 * fees.maker_rate + qty * 95 * fees.taker_rate
    reward = qty * (110 - 100) - qty * 100 * fees.maker_rate - qty * 110 * fees.maker_rate
    assert m.net_rr == pytest.approx(round(reward / loss, 4))
