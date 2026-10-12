from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings


@dataclass
class ExecutionAssumptions:
    fill_price: float
    spread_cost_pct: float
    slippage_cost_pct: float
    fee_open_pct: float
    fee_close_pct: float


@dataclass
class RRCalculation:
    gross_rr: float
    net_rr: float
    risk_usdt: float
    reward_usdt: float
    net_reward_usdt: float
    acceptable: bool


class PaperExecutionEngine:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def notional(self) -> float:
        return self.settings.margin_usdt * self.settings.leverage

    def apply_spread(self, price: float, direction: str, side: str) -> float:
        """side: open or close"""
        bps = self.settings.spread_bps / 10000
        if direction == "LONG":
            return price * (1 + bps) if side == "open" else price * (1 - bps)
        return price * (1 - bps) if side == "open" else price * (1 + bps)

    def apply_slippage(self, price: float, direction: str, adverse: bool) -> float:
        bps = self.settings.slippage_bps / 10000
        if direction == "LONG":
            return price * (1 + bps) if adverse else price * (1 - bps)
        return price * (1 - bps) if adverse else price * (1 + bps)

    def simulate_fill(self, raw_price: float, direction: str, entry_type: str, is_stop_entry: bool) -> float:
        price = self.apply_spread(raw_price, direction, "open")
        adverse = entry_type == "STOP" or is_stop_entry
        return self.apply_slippage(price, direction, adverse=adverse)

    def simulate_exit(self, raw_price: float, direction: str, hit_tp: bool) -> float:
        price = self.apply_spread(raw_price, direction, "close")
        # SL exits are typically adverse; TP slightly less
        return self.apply_slippage(price, direction, adverse=not hit_tp)

    def calc_rr(
        self,
        direction: str,
        entry: float,
        sl: float,
        tp: float,
        min_net_rr: float | None = None,
    ) -> RRCalculation:
        min_net_rr = min_net_rr if min_net_rr is not None else self.settings.min_net_rr
        notional = self.notional()

        if direction == "LONG":
            risk_per_unit = entry - sl
            reward_per_unit = tp - entry
        else:
            risk_per_unit = sl - entry
            reward_per_unit = entry - tp

        if risk_per_unit <= 0 or reward_per_unit <= 0:
            return RRCalculation(0, 0, 0, 0, 0, False)

        gross_rr = reward_per_unit / risk_per_unit
        risk_usdt = (risk_per_unit / entry) * notional
        reward_usdt = (reward_per_unit / entry) * notional

        fee_open = notional * self.settings.taker_fee_rate
        fee_close = notional * self.settings.taker_fee_rate
        slip = notional * (self.settings.slippage_bps / 10000) * 2
        spread = notional * (self.settings.spread_bps / 10000) * 2
        net_reward = reward_usdt - fee_open - fee_close - slip - spread
        net_rr = net_reward / risk_usdt if risk_usdt > 0 else 0

        return RRCalculation(
            gross_rr=round(gross_rr, 3),
            net_rr=round(net_rr, 3),
            risk_usdt=round(risk_usdt, 4),
            reward_usdt=round(reward_usdt, 4),
            net_reward_usdt=round(net_reward, 4),
            acceptable=net_rr >= min_net_rr,
        )

    def pnl_usdt(self, direction: str, fill: float, exit_price: float) -> float:
        notional = self.notional()
        if direction == "LONG":
            raw = ((exit_price - fill) / fill) * notional
        else:
            raw = ((fill - exit_price) / fill) * notional
        fees = notional * self.settings.taker_fee_rate * 2
        return round(raw - fees, 4)
