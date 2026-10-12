from __future__ import annotations

import time
from dataclasses import dataclass

from app.domain.approval_policy import approval_inside_forming_bar, entry_monitor_start_ms
from app.domain.candles import Candle
from app.domain.time_utils import ensure_utc
from app.models import Signal
from app.services.market_data import MarketDataService
from app.services.signal_lifecycle import SignalLifecycleService


@dataclass
class ApprovalPrecheckResult:
    ok: bool
    reason: str | None
    entry_monitor_from_ms: int | None
    touched_candle_ms: int | None
    policy_note: str


class ApprovalPrecheckService:
    def __init__(self, market: MarketDataService | None = None) -> None:
        self.market = market or MarketDataService()
        self.lifecycle = SignalLifecycleService()

    def _touched(self, signal: Signal, bar: Candle) -> bool:
        ok, _ = self.lifecycle.entry_triggered(signal, bar.high, bar.low, bar.close)
        return ok

    async def evaluate(self, signal: Signal, approval_ms: int, now_ms: int) -> ApprovalPrecheckResult:
        created = ensure_utc(signal.created_at)
        if not created:
            return ApprovalPrecheckResult(False, "NO_CREATED_AT", None, None, "")
        created_ms = int(created.timestamp() * 1000)

        bundle = await self.market.fetch_kline_bundle(signal.symbol, "5m", 200)
        if not bundle.data_complete:
            return ApprovalPrecheckResult(False, "INCOMPLETE_MARKET_DATA", None, None, "Gap backfill incomplete")

        for bar in bundle.closed:
            if bar.time_ms < created_ms:
                continue
            if bar.time_ms >= approval_ms:
                break
            if self._touched(signal, bar):
                return ApprovalPrecheckResult(
                    False,
                    "LATE_APPROVAL_ENTRY_TOUCHED",
                    None,
                    bar.time_ms,
                    "Closed 5m bar touched entry before approval",
                )

        if approval_inside_forming_bar(approval_ms, "5m", now_ms):
            step = 5 * 60_000
            forming_open = (approval_ms // step) * step
            m1 = await self.market.fetch_kline_bundle(signal.symbol, "1m", 120)
            for bar in m1.closed:
                if bar.time_ms < forming_open:
                    continue
                if bar.time_ms >= approval_ms:
                    break
                if self._touched(signal, bar):
                    return ApprovalPrecheckResult(
                        False,
                        "LATE_APPROVAL_ENTRY_TOUCHED",
                        None,
                        bar.time_ms,
                        "1m bar in forming 5m window touched entry before approval",
                    )
            monitor_from = entry_monitor_start_ms(approval_ms, "5m")
            note = (
                "Approval during forming 5m candle; monitoring starts next 5m open "
                f"at {monitor_from}"
            )
            return ApprovalPrecheckResult(True, None, monitor_from, None, note)

        monitor_from = entry_monitor_start_ms(approval_ms, "5m")
        return ApprovalPrecheckResult(
            True,
            None,
            monitor_from,
            None,
            "Approval on closed-bar boundary; monitoring from next 5m open",
        )
