"""Net R:R preflight aligned with actual paper fills, fees and capped execution TP."""

from __future__ import annotations

from app.config import Settings, get_effective_settings
from app.domain.execution_tp_cap import apply_execution_take_profit_cap
from app.models import SimPositionSettings
from app.services.execution_model import ExecutionModel, RRCalculation


def execution_model_from_sim_settings(sim: SimPositionSettings) -> ExecutionModel:
    """Paper fill engine does not charge a second synthetic spread."""
    base: Settings = get_effective_settings().model_copy()
    base.margin_usdt = float(sim.default_margin_usdt)
    base.leverage = int(sim.default_leverage)
    base.taker_fee_rate = float(sim.taker_fee_rate)
    base.maker_fee_rate = float(sim.maker_fee_rate)
    base.slippage_bps = float(sim.slippage_bps)
    base.spread_bps = 0.0
    return ExecutionModel(base)


def auto_paper_rr_from_settings(
    sim: SimPositionSettings,
    *,
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    min_net_rr: float,
    entry_fill_price: float | None = None,
    order_type: str = "limit",
) -> RRCalculation:
    """Evaluate the EXECUTABLE TP, not the AI's original larger target.

    On a filled order, entry_fill_price is the actual persisted entry price,
    so entry slippage is not applied twice.
    """
    ref_entry = float(entry_fill_price) if entry_fill_price is not None else entry
    capped = apply_execution_take_profit_cap(
        direction=direction,
        entry=ref_entry,
        stop_loss=sl,
        take_profit_ai=tp,
    )
    model = execution_model_from_sim_settings(sim)
    is_limit_entry = order_type.lower() == "limit"
    entry_rate = float(sim.maker_fee_rate if is_limit_entry else sim.taker_fee_rate)
    tp_rate = (
        float(sim.maker_fee_rate) if sim.default_tp_behavior == "limit"
        else float(sim.taker_fee_rate)
    )
    return model.calc_rr(
        direction, ref_entry, sl, capped.execution_take_profit,
        "LIMIT" if is_limit_entry else "STOP", min_net_rr=min_net_rr,
        entry_fee_rate=entry_rate,
        tp_fee_rate=tp_rate,
        sl_fee_rate=float(sim.taker_fee_rate),
        # Never assume the maker LIMIT improves the requested entry or that
        # a TP LIMIT fills above/below its specified price. Execution may do
        # better, but the 2R acceptance gate must remain conservative.
        entry_fill_override=(
            ref_entry if is_limit_entry or entry_fill_price is not None else None
        ),
        tp_exit_fill_override=capped.execution_take_profit,
    )


async def net_rr_acceptable_for_auto_paper(
    session,
    *,
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    min_net_rr: float,
    entry_fill_price: float | None = None,
    order_type: str = "limit",
) -> tuple[bool, float]:
    from app.services.position.settings_service import get_or_create_settings

    sim = await get_or_create_settings(session)
    rr = auto_paper_rr_from_settings(
        sim, direction=direction, entry=entry, sl=sl, tp=tp,
        min_net_rr=min_net_rr, entry_fill_price=entry_fill_price,
        order_type=order_type,
    )
    return bool(rr.acceptable), float(rr.net_rr)
