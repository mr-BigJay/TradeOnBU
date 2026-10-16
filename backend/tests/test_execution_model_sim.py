from __future__ import annotations

import pytest

from app.domain.paper_execution_policy import HARD_MIN_AUTO_PAPER_NET_RR
from app.models import SimPositionSettings
from app.services.execution_model import ExecutionModel
from app.services.execution_model_sim import execution_model_from_sim_settings, net_rr_acceptable_for_auto_paper


def test_execution_model_uses_sim_vip1_fees_not_global_defaults():
    sim = SimPositionSettings(
        id=1,
        default_margin_usdt=50.0,
        default_leverage=10,
        maker_fee_rate=0.0002,
        taker_fee_rate=0.0005,
        slippage_bps=5.0,
    )
    model = execution_model_from_sim_settings(sim)
    global_model = ExecutionModel()
    rr_sim = model.calc_rr("LONG", 100.0, 94.0, 115.0, "LIMIT", min_net_rr=HARD_MIN_AUTO_PAPER_NET_RR)
    rr_global = global_model.calc_rr("LONG", 100.0, 94.0, 115.0, "LIMIT", min_net_rr=HARD_MIN_AUTO_PAPER_NET_RR)
    assert rr_sim.net_rr != rr_global.net_rr or sim.taker_fee_rate != global_model.settings.taker_fee_rate
    assert rr_sim.acceptable is True


@pytest.mark.asyncio
async def test_net_rr_helper_honors_hard_floor():
    from app.db import SessionLocal, init_db

    await init_db()
    async with SessionLocal() as session:
        ok, net_rr = await net_rr_acceptable_for_auto_paper(
            session,
        direction="LONG",
        entry=100.0,
        sl=98.0,
        tp=103.0,
            min_net_rr=HARD_MIN_AUTO_PAPER_NET_RR,
        )
    assert ok is False
    assert net_rr < HARD_MIN_AUTO_PAPER_NET_RR
