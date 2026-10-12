from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings


@dataclass
class RRCalculation:
    gross_rr: float
    net_rr: float
    risk_usdt: float
    reward_usdt: float
    net_reward_usdt: float
    net_loss_usdt: float
    acceptable: bool
    entry_fill: float
    tp_exit_fill: float
    sl_exit_fill: float


class PaperExecutionEngine:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def notional_at_price(self, price: float) -> float:
        return self.settings.margin_usdt * self.settings.leverage

    def apply_spread(self, price: float, direction: str, side: str) -> float:
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
        return self.apply_slippage(price, direction, adverse=not hit_tp)

    def _net_pnl(self, direction: str, entry_fill: float, exit_fill: float) -> float:
        qty = self.notional_at_price(entry_fill) / entry_fill
        if direction == "LONG":
            gross = qty * (exit_fill - entry_fill)
        else:
            gross = qty * (entry_fill - exit_fill)
        entry_fee = entry_fill * qty * self.settings.taker_fee_rate
        exit_fee = exit_fill * qty * self.settings.taker_fee_rate
        return gross - entry_fee - exit_fee

    def calc_rr(
        self,
        direction: str,
        entry: float,
        sl: float,
        tp: float,
        min_net_rr: float | None = None,
    ) -> RRCalculation:
        min_net_rr = min_net_rr if min_net_rr is not None else self.settings.min_net_rr

        if direction == "LONG":
            if not (sl < entry < tp):
                return RRCalculation(0, 0, 0, 0, 0, 0, False, entry, tp, sl)
        elif not (tp < entry < sl):
            return RRCalculation(0, 0, 0, 0, 0, 0, False, entry, tp, sl)

        entry_fill = self.simulate_fill(entry, direction, "LIMIT", False)
        sl_exit = self.simulate_exit(sl, direction, hit_tp=False)
        tp_exit = self.simulate_exit(tp, direction, hit_tp=True)

        net_tp = self._net_pnl(direction, entry_fill, tp_exit)
        net_sl = self._net_pnl(direction, entry_fill, sl_exit)

        gross_move_risk = abs(entry - sl)
        gross_move_reward = abs(tp - entry)
        gross_rr = gross_move_reward / gross_move_risk if gross_move_risk > 0 else 0

        net_loss_usdt = net_sl if net_sl < 0 else -abs(net_sl)
        net_reward_usdt = net_tp
        net_rr = net_reward_usdt / abs(net_loss_usdt) if net_loss_usdt != 0 else 0

        return RRCalculation(
            gross_rr=round(gross_rr, 3),
            net_rr=round(net_rr, 3),
            risk_usdt=round(abs(net_loss_usdt), 4),
            reward_usdt=round(gross_move_reward / entry * self.notional_at_price(entry), 4),
            net_reward_usdt=round(net_reward_usdt, 4),
            net_loss_usdt=round(net_loss_usdt, 4),
            acceptable=net_rr >= min_net_rr and net_tp > 0 and net_sl < 0,
            entry_fill=round(entry_fill, 6),
            tp_exit_fill=round(tp_exit, 6),
            sl_exit_fill=round(sl_exit, 6),
        )

    def pnl_usdt(self, direction: str, fill: float, exit_price: float) -> float:
        exit_fill = self.simulate_exit(exit_price, direction, hit_tp=exit_price != fill)
        return round(self._net_pnl(direction, fill, exit_fill), 4)
