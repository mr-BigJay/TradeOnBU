from __future__ import annotations

from dataclasses import dataclass

from app.services.position.fee_config import FeeProfile


@dataclass
class PositionMetrics:
    notional_usdt: float
    quantity_btc: float
    entry_fee_usdt: float
    estimated_exit_fee_usdt: float
    gross_unrealized_pnl: float
    net_unrealized_pnl: float
    pnl_pct_on_margin: float
    sl_distance_pct: float
    tp_distance_pct: float
    gross_rr: float
    net_rr: float
    liquidation_price: float | None
    liquidation_is_estimate: bool


def position_notional(margin_usdt: float, leverage: int) -> float:
    return margin_usdt * leverage


def quantity_from_notional(notional: float, price: float) -> float:
    if price <= 0:
        return 0.0
    return notional / price


def fee_on_notional(notional: float, price: float, fee_rate: float) -> float:
    """Fee from executed notional at given price (qty * price * rate)."""
    qty = quantity_from_notional(notional, price)
    return qty * price * fee_rate


def gross_pnl(direction: str, entry: float, mark: float, qty: float) -> float:
    if direction == "LONG":
        return qty * (mark - entry)
    return qty * (entry - mark)


def pct_distance(from_price: float, to_price: float) -> float:
    if from_price <= 0:
        return 0.0
    return abs(to_price - from_price) / from_price * 100.0


def rr_ratio(entry: float, sl: float, tp: float, direction: str) -> float:
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0:
        return 0.0
    if direction == "LONG" and not (sl < entry < tp):
        return 0.0
    if direction == "SHORT" and not (tp < entry < sl):
        return 0.0
    return reward / risk


def estimate_isolated_liquidation(
    direction: str,
    entry: float,
    leverage: int,
    maintenance_margin_rate: float = 0.005,
) -> tuple[float | None, bool]:
    """Conservative isolated-margin estimate; not verified against Bitunix engine."""
    if entry <= 0 or leverage <= 0:
        return None, True
    imr = 1.0 / leverage
    if direction == "LONG":
        liq = entry * (1.0 - imr + maintenance_margin_rate)
        return max(liq, 0.0), True
    liq = entry * (1.0 + imr - maintenance_margin_rate)
    return liq, True


def compute_metrics(
    *,
    direction: str,
    margin_usdt: float,
    leverage: int,
    entry_price: float,
    mark_price: float,
    stop_loss: float,
    take_profit: float,
    fees: FeeProfile,
    entry_is_maker: bool,
    exit_is_maker: bool,
) -> PositionMetrics:
    notional = position_notional(margin_usdt, leverage)
    qty = quantity_from_notional(notional, entry_price)
    entry_rate = fees.maker_rate if entry_is_maker else fees.taker_rate
    exit_rate = fees.maker_rate if exit_is_maker else fees.taker_rate
    entry_fee = fee_on_notional(notional, entry_price, entry_rate)
    exit_fee_est = fee_on_notional(notional, mark_price, exit_rate)
    gross_u = gross_pnl(direction, entry_price, mark_price, qty)
    net_u = gross_u - entry_fee - exit_fee_est
    pnl_pct = (net_u / margin_usdt * 100.0) if margin_usdt > 0 else 0.0
    gross = rr_ratio(entry_price, stop_loss, take_profit, direction)
    risk_usdt = abs(gross_pnl(direction, entry_price, stop_loss, qty)) - entry_fee
    reward_usdt = gross_pnl(direction, entry_price, take_profit, qty) - exit_fee_est
    net_rr = reward_usdt / risk_usdt if risk_usdt > 0 else 0.0
    liq, est = estimate_isolated_liquidation(direction, entry_price, leverage)
    return PositionMetrics(
        notional_usdt=round(notional, 4),
        quantity_btc=round(qty, 8),
        entry_fee_usdt=round(entry_fee, 6),
        estimated_exit_fee_usdt=round(exit_fee_est, 6),
        gross_unrealized_pnl=round(gross_u, 6),
        net_unrealized_pnl=round(net_u, 6),
        pnl_pct_on_margin=round(pnl_pct, 4),
        sl_distance_pct=round(pct_distance(entry_price, stop_loss), 4),
        tp_distance_pct=round(pct_distance(entry_price, take_profit), 4),
        gross_rr=round(gross, 4),
        net_rr=round(net_rr, 4),
        liquidation_price=round(liq, 2) if liq else None,
        liquidation_is_estimate=est,
    )
