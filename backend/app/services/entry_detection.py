from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from app.models import EntryType, TrendState
from app.services.ict_structure import analyze_structure, validate_ict_sequence
from app.services.market_data import MarketDataService
from app.services.paper_execution import PaperExecutionEngine
from app.services.ai_first_mode import legacy_rule_scan_enabled
from app.services.trend_engine import TrendEngine


@dataclass
class SetupCandidate:
    symbol: str
    direction: str
    entry_type: str
    entry_price: float
    entry_zone_low: float
    entry_zone_high: float
    stop_loss: float
    take_profit: float
    invalidation_level: float
    market_bias: str
    liquidity_sweep: bool
    mss_confirmation: bool
    fvg_entry: bool
    gross_rr: float
    net_rr: float
    estimated_holding_hours: float
    analysis: dict
    dedupe_key: str
    rejected_reason: str | None = None


def _reject(symbol: str, direction: str, reason: str, analysis: dict) -> SetupCandidate:
    return SetupCandidate(
        symbol=symbol,
        direction=direction,
        entry_type=EntryType.LIMIT.value,
        entry_price=0,
        entry_zone_low=0,
        entry_zone_high=0,
        stop_loss=0,
        take_profit=0,
        invalidation_level=0,
        market_bias="",
        liquidity_sweep=False,
        mss_confirmation=False,
        fvg_entry=False,
        gross_rr=0,
        net_rr=0,
        estimated_holding_hours=6.0,
        analysis=analysis,
        dedupe_key="",
        rejected_reason=reason,
    )


class EntryDetectionService:
    def __init__(
        self,
        market: MarketDataService,
        trend_engine: TrendEngine,
        paper: PaperExecutionEngine,
    ) -> None:
        self.market = market
        self.trend_engine = trend_engine
        self.paper = paper

    async def scan(self, symbol: str, min_net_rr: float) -> SetupCandidate | None:
        if not legacy_rule_scan_enabled():
            return None
        ctx = await self.trend_engine.analyze(symbol)
        if ctx.ict_bias not in (TrendState.BULLISH, TrendState.BEARISH):
            return None

        c15 = await self.market.fetch_klines(symbol, "15m", 200)
        c5 = await self.market.fetch_klines(symbol, "5m", 200)
        s15 = analyze_structure(c15)
        s5 = analyze_structure(c5)

        if not s15.has_clear_structure or not s5.has_clear_structure:
            return None

        direction = "LONG" if ctx.ict_bias == TrendState.BULLISH else "SHORT"
        if ctx.trend_4h == TrendState.BEARISH and direction == "LONG":
            return None
        if ctx.trend_4h == TrendState.BULLISH and direction == "SHORT":
            return None

        seq = validate_ict_sequence(c5, direction)
        if not seq.ok or not seq.fvg:
            return _reject(
                symbol,
                direction,
                "NO_TRADE",
                {"ict_sequence": seq.__dict__, "reason": seq.reason},
            )

        if not seq.liquidity_sweep or not seq.mss_confirmation or not seq.displacement or not seq.fvg_valid:
            return _reject(
                symbol,
                direction,
                "NO_TRADE",
                {"ict_sequence": seq.__dict__, "reason": "MISSING_REQUIRED_ICT_CONFIRMATIONS"},
            )

        fvg = seq.fvg
        zone_low, zone_high = fvg.low, fvg.high
        entry_price = (zone_low + zone_high) / 2

        swings = s5.swings
        if direction == "LONG":
            recent_lows = [s.price for s in swings if s.kind == "LOW"]
            if not recent_lows:
                return _reject(symbol, direction, "NO_TRADE", {"reason": "NO_SWING_LOW"})
            invalidation = min(recent_lows[-2:])
            stop_loss = invalidation * 0.999
            liq = s5.liquidity_levels
            tp_candidates = [lv for lv in liq if lv > entry_price]
            if not tp_candidates:
                return _reject(symbol, direction, "NO_TRADE", {"reason": "NO_STRUCTURAL_LIQUIDITY_TP"})
            take_profit = min(tp_candidates)
        else:
            recent_highs = [s.price for s in swings if s.kind == "HIGH"]
            if not recent_highs:
                return _reject(symbol, direction, "NO_TRADE", {"reason": "NO_SWING_HIGH"})
            invalidation = max(recent_highs[-2:])
            stop_loss = invalidation * 1.001
            liq = s5.liquidity_levels
            tp_candidates = [lv for lv in liq if lv < entry_price]
            if not tp_candidates:
                return _reject(symbol, direction, "NO_TRADE", {"reason": "NO_STRUCTURAL_LIQUIDITY_TP"})
            take_profit = max(tp_candidates)

        rr = self.paper.calc_rr(direction, entry_price, stop_loss, take_profit, EntryType.LIMIT.value, min_net_rr)
        if not rr.acceptable:
            return _reject(
                symbol,
                direction,
                "NO_TRADE_RR",
                {"net_rr": rr.net_rr, "min_net_rr": min_net_rr},
            )

        dedupe_raw = f"{symbol}:{direction}:{round(entry_price, 1)}:{round(stop_loss, 1)}:{round(take_profit, 1)}"
        dedupe_key = hashlib.sha256(dedupe_raw.encode()).hexdigest()[:32]

        return SetupCandidate(
            symbol=symbol,
            direction=direction,
            entry_type=EntryType.LIMIT.value,
            entry_price=round(entry_price, 2),
            entry_zone_low=round(zone_low, 2),
            entry_zone_high=round(zone_high, 2),
            stop_loss=round(stop_loss, 2),
            take_profit=round(take_profit, 2),
            invalidation_level=round(invalidation, 2),
            market_bias=ctx.ict_bias.value,
            liquidity_sweep=True,
            mss_confirmation=True,
            fvg_entry=True,
            gross_rr=rr.gross_rr,
            net_rr=rr.net_rr,
            estimated_holding_hours=6.0,
            analysis={
                "trend_context": {
                    "4h": ctx.trend_4h.value,
                    "15m": ctx.trend_15m.value,
                    "5m": ctx.trend_5m.value,
                    "bias": ctx.ict_bias.value,
                },
                "ict_sequence": {
                    "sweep_index": seq.sweep_index,
                    "mss_index": seq.mss_index,
                    "displacement_index": seq.displacement_index,
                    "liquidity_level": seq.liquidity_level,
                },
                "structure_15m": s15.summary,
                "structure_5m": s5.summary,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            dedupe_key=dedupe_key,
        )
