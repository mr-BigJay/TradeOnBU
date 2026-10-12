from __future__ import annotations

from dataclasses import dataclass

from app.domain.approval_policy import approval_inside_forming_bar, entry_monitor_start_ms
from app.domain.candle_utils import interval_ms
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

    async def _scan_1m_window(
        self,
        signal: Signal,
        m1_closed: list[Candle],
        start_ms: int,
        end_ms: int,
    ) -> ApprovalPrecheckResult | None:
        for bar in m1_closed:
            if bar.time_ms < start_ms:
                continue
            if bar.time_ms >= end_ms:
                break
            if self._touched(signal, bar):
                return ApprovalPrecheckResult(
                    False,
                    "LATE_APPROVAL_ENTRY_TOUCHED",
                    None,
                    bar.time_ms,
                    f"1m bar touched entry between {start_ms} and {end_ms}",
                )
        return None

    async def evaluate(self, signal: Signal, approval_ms: int, now_ms: int) -> ApprovalPrecheckResult:
        created = ensure_utc(signal.created_at)
        if not created:
            return ApprovalPrecheckResult(False, "NO_CREATED_AT", None, None, "")
        created_ms = int(created.timestamp() * 1000)

        bundle = await self.market.fetch_kline_bundle(signal.symbol, "5m", 200)
        if not bundle.data_complete:
            return ApprovalPrecheckResult(False, "INCOMPLETE_MARKET_DATA", None, None, "Gap backfill incomplete")

        step_5m = interval_ms("5m")
        creation_bar_open = (created_ms // step_5m) * step_5m
        creation_bar_close = creation_bar_open + step_5m

        m1_bundle = await self.market.fetch_kline_bundle(signal.symbol, "1m", 300)
        if not m1_bundle.data_complete:
            return ApprovalPrecheckResult(
                False,
                "APPROVAL_TIMING_UNCERTAIN",
                None,
                None,
                "Incomplete 1m history — cannot verify creation-boundary entry touch",
            )

        step_1m = interval_ms("1m")
        creation_1m_open = (created_ms // step_1m) * step_1m
        if created_ms > creation_1m_open:
            creation_1m = next((b for b in m1_bundle.closed if b.time_ms == creation_1m_open), None)
            if creation_1m and self._touched(signal, creation_1m):
                return ApprovalPrecheckResult(
                    False,
                    "APPROVAL_ENTRY_TOUCH_AMBIGUOUS",
                    None,
                    creation_1m.time_ms,
                    "Signal created mid-1m; OHLC cannot prove entry was not touched before creation",
                )

        # Partial 5m candle at signal creation: open before created_ms, touch may occur after creation.
        if creation_bar_open <= created_ms < creation_bar_close and approval_ms > created_ms:
            partial_end = min(approval_ms, creation_bar_close)
            hit = await self._scan_1m_window(signal, m1_bundle.closed, created_ms, partial_end)
            if hit:
                return hit

        # Fully closed 5m bars after the creation candle.
        for bar in bundle.closed:
            if bar.time_ms < creation_bar_close:
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
            forming_open = (approval_ms // step_5m) * step_5m
            hit = await self._scan_1m_window(signal, m1_bundle.closed, forming_open, approval_ms)
            if hit:
                return hit
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
