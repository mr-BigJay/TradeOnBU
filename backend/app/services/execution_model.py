from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.config import Settings, get_effective_settings, get_settings


class ExitOutcome(str, Enum):
    TP = "TP"
    SL = "SL"
    TIME = "TIME"


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


class ExecutionModel:
    """Single source of truth for paper fills, costs, P&L and net R:R."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_effective_settings()

    @classmethod
    def for_signal(cls, signal) -> ExecutionModel:
        params = (getattr(signal, "analysis", None) or {}).get("position_params") or {}
        if not params:
            return cls()
        base = get_effective_settings().model_copy()
        for key, val in params.items():
            if hasattr(base, key):
                setattr(base, key, type(getattr(base, key))(val))
        return cls(base)

    def margin_notional(self) -> float:
        return self.settings.margin_usdt * self.settings.leverage

    def _spread_price(self, price: float, direction: str, side: str) -> float:
        bps = self.settings.spread_bps / 10000
        if direction == "LONG":
            return price * (1 + bps) if side == "open" else price * (1 - bps)
        return price * (1 - bps) if side == "open" else price * (1 + bps)

    def _slippage_price(self, price: float, direction: str, adverse: bool, *, side: str = "open") -> float:
        # Direction is the POSITION direction, not the side of every trade.
        # A LONG exit sells and a SHORT exit buys (reverse the entry slippage).
        bps = self.settings.slippage_bps / 10000
        factor = bps if adverse else -bps
        if direction == "LONG":
            return price * (1 + factor) if side == "open" else price * (1 - factor)
        return price * (1 - factor) if side == "open" else price * (1 + factor)

    def open_fill(self, raw_price: float, direction: str, entry_type: str) -> float:
        spread = self._spread_price(raw_price, direction, "open")
        adverse = entry_type == "STOP"
        return self._slippage_price(spread, direction, adverse=adverse)

    def close_fill(self, raw_price: float, direction: str, outcome: ExitOutcome) -> float:
        spread = self._spread_price(raw_price, direction, "close")
        adverse = outcome in (ExitOutcome.SL, ExitOutcome.TIME)
        return self._slippage_price(spread, direction, adverse=adverse, side="close")

    def net_pnl(
        self, direction: str, entry_fill: float, exit_fill: float,
        *, entry_fee_rate: float | None = None, exit_fee_rate: float | None = None,
    ) -> float:
        qty = self.margin_notional() / entry_fill
        if direction == "LONG":
            gross = qty * (exit_fill - entry_fill)
        else:
            gross = qty * (entry_fill - exit_fill)
        entry_rate = self.settings.taker_fee_rate if entry_fee_rate is None else entry_fee_rate
        exit_rate = self.settings.taker_fee_rate if exit_fee_rate is None else exit_fee_rate
        fees = (entry_fill * entry_rate + exit_fill * exit_rate) * qty
        return gross - fees

    def calc_rr(
        self,
        direction: str,
        entry: float,
        sl: float,
        tp: float,
        entry_type: str = "LIMIT",
        min_net_rr: float | None = None,
        *,
        entry_fee_rate: float | None = None,
        tp_fee_rate: float | None = None,
        sl_fee_rate: float | None = None,
        entry_fill_override: float | None = None,
    ) -> RRCalculation:
        min_net_rr = min_net_rr if min_net_rr is not None else self.settings.min_net_rr
        if direction == "LONG" and not (sl < entry < tp):
            return RRCalculation(0, 0, 0, 0, 0, 0, False, entry, tp, sl)
        if direction == "SHORT" and not (tp < entry < sl):
            return RRCalculation(0, 0, 0, 0, 0, 0, False, entry, tp, sl)

        entry_fill = (
            float(entry_fill_override) if entry_fill_override is not None
            else self.open_fill(entry, direction, entry_type)
        )
        if entry_fill <= 0:
            return RRCalculation(0, 0, 0, 0, 0, 0, False, entry_fill, tp, sl)
        sl_fill = self.close_fill(sl, direction, ExitOutcome.SL)
        tp_fill = self.close_fill(tp, direction, ExitOutcome.TP)
        net_sl = self.net_pnl(
            direction, entry_fill, sl_fill,
            entry_fee_rate=entry_fee_rate, exit_fee_rate=sl_fee_rate,
        )
        net_tp = self.net_pnl(
            direction, entry_fill, tp_fill,
            entry_fee_rate=entry_fee_rate, exit_fee_rate=tp_fee_rate,
        )

        gross_rr = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
        net_loss_usdt = net_sl if net_sl < 0 else -abs(net_sl)
        net_rr = net_tp / abs(net_loss_usdt) if net_loss_usdt else 0

        return RRCalculation(
            gross_rr=round(gross_rr, 3),
            net_rr=round(net_rr, 3),
            risk_usdt=round(abs(net_loss_usdt), 4),
            reward_usdt=round(abs(tp - entry) / entry * self.margin_notional(), 4),
            net_reward_usdt=round(net_tp, 4),
            net_loss_usdt=round(net_loss_usdt, 4),
            acceptable=net_rr >= min_net_rr and net_tp > 0 and net_sl < 0,
            entry_fill=round(entry_fill, 6),
            tp_exit_fill=round(tp_fill, 6),
            sl_exit_fill=round(sl_fill, 6),
        )

    def pnl_from_raw_exit(self, direction: str, entry_fill: float, raw_exit: float, outcome: ExitOutcome) -> float:
        exit_fill = self.close_fill(raw_exit, direction, outcome)
        return round(self.net_pnl(direction, entry_fill, exit_fill), 4)


# Backwards-compatible alias used across services/tests
PaperExecutionEngine = ExecutionModel
