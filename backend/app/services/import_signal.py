from __future__ import annotations

import hashlib
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Signal, SignalSource, SignalStatus
from app.schemas import ChatGPTImportRequest
from app.services.entry_detection import SetupCandidate
from app.services.paper_execution import PaperExecutionEngine
from app.services.signal_lifecycle import SignalLifecycleService
from app.services.trend_engine import TrendEngine


class ImportSignalService:
    def __init__(
        self,
        trend_engine: TrendEngine,
        paper: PaperExecutionEngine,
        lifecycle: SignalLifecycleService,
    ) -> None:
        self.trend_engine = trend_engine
        self.paper = paper
        self.lifecycle = lifecycle

    async def validate_and_create(
        self,
        session: AsyncSession,
        req: ChatGPTImportRequest,
        min_net_rr: float,
        max_pending_minutes: int,
        holding_time_hours: float,
        require_telegram: bool,
    ) -> tuple[Signal | None, str | None]:
        ctx = await self.trend_engine.analyze(req.symbol)
        quote = await self.trend_engine.market.fetch_live_price(req.symbol)
        price = quote.price

        # Price validity: entry within 0.5% of live for manual import
        if abs(price - req.entry_price) / req.entry_price * 100 > 0.5:
            return None, "Entry price too far from live market (>0.5%)"

        invalidation = req.invalidation_level
        if invalidation is None:
            invalidation = req.stop_loss

        direction = req.direction
        if direction == "LONG" and req.stop_loss >= req.entry_price:
            return None, "Invalid SL for LONG"
        if direction == "SHORT" and req.stop_loss <= req.entry_price:
            return None, "Invalid SL for SHORT"

        rr = self.paper.calc_rr(direction, req.entry_price, req.stop_loss, req.take_profit, req.entry_type, min_net_rr)
        if not rr.acceptable:
            return None, f"Net R:R {rr.net_rr} below minimum {min_net_rr}"

        bias = req.market_bias or ctx.ict_bias.value
        if direction == "LONG" and bias == "BEARISH":
            return None, "Direction conflicts with bearish bias"
        if direction == "SHORT" and bias == "BULLISH":
            return None, "Direction conflicts with bullish bias"

        dedupe_raw = f"{req.symbol}:{direction}:{round(req.entry_price, 1)}:{round(req.stop_loss, 1)}"
        dedupe_key = hashlib.sha256(dedupe_raw.encode()).hexdigest()[:32]

        candidate = SetupCandidate(
            symbol=req.symbol,
            direction=direction,
            entry_type=req.entry_type,
            entry_price=req.entry_price,
            entry_zone_low=req.entry_zone_low or req.entry_price,
            entry_zone_high=req.entry_zone_high or req.entry_price,
            stop_loss=req.stop_loss,
            take_profit=req.take_profit,
            invalidation_level=invalidation,
            market_bias=bias,
            liquidity_sweep=req.liquidity_sweep,
            mss_confirmation=req.mss_confirmation,
            fvg_entry=req.fvg_entry,
            gross_rr=rr.gross_rr,
            net_rr=rr.net_rr,
            estimated_holding_hours=6.0,
            analysis={"import_notes": req.notes, "manual_review": "ChatGPT Import", "live_price": price},
            dedupe_key=dedupe_key,
        )
        sig = await self.lifecycle.create_from_candidate(
            session, candidate, max_pending_minutes, require_telegram, holding_time_hours
        )
        if sig:
            sig.source = SignalSource.CHATGPT_IMPORT.value
            sig.status = SignalStatus.WAITING_FOR_ENTRY.value
            await session.commit()
        return sig, None
