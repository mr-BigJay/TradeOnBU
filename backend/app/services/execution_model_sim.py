"""ExecutionModel configured from SimPositionSettings (paper trading cost basis)."""

from __future__ import annotations

from app.config import Settings, get_effective_settings
from app.models import SimPositionSettings
from app.services.execution_model import ExecutionModel


def execution_model_from_sim_settings(sim: SimPositionSettings) -> ExecutionModel:
    """Align net R:R checks with paper fill fees, slippage, and margin/leverage."""
    base: Settings = get_effective_settings().model_copy()
    base.margin_usdt = float(sim.default_margin_usdt)
    base.leverage = int(sim.default_leverage)
    base.taker_fee_rate = float(sim.taker_fee_rate)
    base.maker_fee_rate = float(sim.maker_fee_rate)
    base.slippage_bps = float(sim.slippage_bps)
    return ExecutionModel(base)


async def net_rr_acceptable_for_auto_paper(
    session,
    *,
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    min_net_rr: float,
) -> tuple[bool, float]:
    from app.services.position.settings_service import get_or_create_settings

    sim = await get_or_create_settings(session)
    model = execution_model_from_sim_settings(sim)
    rr = model.calc_rr(direction, entry, sl, tp, "LIMIT", min_net_rr=min_net_rr)
    return bool(rr.acceptable), float(rr.net_rr)
