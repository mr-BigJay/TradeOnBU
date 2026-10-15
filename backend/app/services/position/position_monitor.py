from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.candles import Candle
from app.config import get_effective_settings
from app.domain.execution_order_policy import (
    bar_hits_limit,
    reconcile_pending_ai_limit_order,
    verify_five_m_for_pending_fills,
)
from app.domain.time_utils import ensure_utc
from app.models import SimPosition
from app.services.market_data import KlineBundle, MarketDataService
from app.services.position.equity_service import record_equity_snapshot
from app.services.position.monitor_gap_utils import (
    INTERVAL_1M_MS,
    can_resume_from_suspension,
    eligible_bars_after_cursor,
    leading_history_gap,
    missing_interval_before_bar,
)
from app.services.position.monitor_policies import (
    EXECUTION_NORMAL,
    EXECUTION_SUSPENDED,
    POLICY_GAP_UNRESOLVED,
    POLICY_SAME_BAR_ENTRY,
    same_bar_exit_ambiguous_after_entry,
)
from app.services.position.position_service import SimPositionService
from app.services.position.settings_service import get_or_create_settings

MAX_RECOVERY_MINUTES = 24 * 60


def _bar_hits_limit(bar: Candle, limit: float) -> bool:
    return bar.low <= limit <= bar.high


def _tp_sl_on_bar(direction: str, bar: Candle, sl: float, tp: float) -> tuple[str | None, float | None, bool]:
    """Returns (reason, exit_price, ambiguous). Only for bars after entry is known."""
    if direction == "LONG":
        sl_hit = bar.low <= sl
        tp_hit = bar.high >= tp
        if sl_hit and tp_hit:
            return None, None, True
        if sl_hit:
            return "sl_hit", sl, False
        if tp_hit:
            return "tp_hit", tp, False
    else:
        sl_hit = bar.high >= sl
        tp_hit = bar.low <= tp
        if sl_hit and tp_hit:
            return None, None, True
        if sl_hit:
            return "sl_hit", sl, False
        if tp_hit:
            return "tp_hit", tp, False
    return None, None, False


def _liquidation_on_bar(direction: str, bar: Candle, liq: float | None) -> bool:
    if liq is None:
        return False
    if direction == "LONG":
        return bar.low <= liq
    return bar.high >= liq


class SimPositionMonitorService:
    def __init__(self, market: MarketDataService | None = None) -> None:
        self.market = market or MarketDataService()
        self.positions = SimPositionService(self.market)

    def _kline_limit_for_positions(self, positions: list[SimPosition]) -> int:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        worst_gap_min = 120
        for pos in positions:
            cursor = pos.last_processed_candle_ms or pos.monitor_from_ms or now_ms
            gap_min = max(0, (now_ms - cursor) // 60000)
            worst_gap_min = max(worst_gap_min, int(gap_min) + 2)
        return min(worst_gap_min + 5, MAX_RECOVERY_MINUTES)

    def _record_gap_event(
        self,
        pos: SimPosition,
        *,
        kind: str,
        cursor_ms: int,
        missing_from_ms: int | None,
        detail: dict,
    ) -> None:
        gaps = list(pos.extra.get("monitor_gaps") or [])
        gaps.append(
            {
                "kind": kind,
                "cursor_ms": cursor_ms,
                "missing_from_ms": missing_from_ms,
                **detail,
                "noted_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        pos.execution_state = EXECUTION_SUSPENDED
        pos.extra = {
            **pos.extra,
            "monitor_gaps": gaps[-30:],
            "execution_suspended_reason": kind,
            "settlement_policy": POLICY_GAP_UNRESOLVED,
        }

    def _suspend_leading_gap(
        self, pos: SimPosition, cursor_ms: int, first_eligible_ms: int, bundle: KlineBundle
    ) -> None:
        self._record_gap_event(
            pos,
            kind="leading_history_gap",
            cursor_ms=cursor_ms,
            missing_from_ms=cursor_ms + INTERVAL_1M_MS,
            detail={
                "first_eligible_ms": first_eligible_ms,
                "gaps_remaining": list(bundle.gaps_remaining),
            },
        )

    def _suspend_internal_gap(
        self,
        pos: SimPosition,
        cursor_ms: int,
        missing_from_ms: int,
        next_bar_ms: int,
        bundle: KlineBundle,
    ) -> None:
        self._record_gap_event(
            pos,
            kind="internal_candle_gap",
            cursor_ms=cursor_ms,
            missing_from_ms=missing_from_ms,
            detail={
                "blocked_bar_ms": next_bar_ms,
                "gaps_remaining": list(bundle.gaps_remaining),
            },
        )

    def _try_clear_suspension(self, pos: SimPosition, bundle: KlineBundle, closed: list[Candle]) -> bool:
        if pos.execution_state != EXECUTION_SUSPENDED:
            return True
        cursor = pos.last_processed_candle_ms or 0
        monitor_from = pos.monitor_from_ms or 0
        if not can_resume_from_suspension(cursor, closed, monitor_from, bundle.gaps_remaining, INTERVAL_1M_MS):
            return False
        pos.execution_state = EXECUTION_NORMAL
        pos.extra = {
            **pos.extra,
            "execution_suspended_reason": None,
            "history_recovered_at": datetime.now(timezone.utc).isoformat(),
        }
        return True

    def _should_block_bar_for_gaps(
        self, pos: SimPosition, bar: Candle, bundle: KlineBundle
    ) -> bool:
        """Block definitive processing when bundle still reports a gap in the window ending at this bar."""
        cursor = pos.last_processed_candle_ms or 0
        from app.services.position.monitor_gap_utils import gaps_remaining_block_processing

        return gaps_remaining_block_processing(
            bundle.gaps_remaining, cursor, bar.time_ms, INTERVAL_1M_MS
        )

    async def _handle_open_bar(
        self,
        session: AsyncSession,
        pos: SimPosition,
        settings,
        bar: Candle,
    ) -> bool:
        """Returns True if position closed on this bar."""
        from app.services.position.calculations import estimate_isolated_liquidation

        entry = pos.fill_price or pos.entry_price or 0.0
        liq, _ = estimate_isolated_liquidation(pos.direction, entry, pos.leverage)
        if _liquidation_on_bar(pos.direction, bar, liq):
            await self.positions._close_position(
                session,
                pos,
                settings,
                raw_exit=liq or bar.close,
                reason="liquidated",
                exit_adverse=True,
                bar_ms=bar.time_ms,
            )
            return True

        reason, raw_exit, ambiguous = _tp_sl_on_bar(pos.direction, bar, pos.stop_loss, pos.take_profit)
        if ambiguous:
            await self.positions._close_position(
                session,
                pos,
                settings,
                raw_exit=bar.close,
                reason="ambiguous_intrabar",
                exit_adverse=True,
                bar_ms=bar.time_ms,
                ambiguous=True,
            )
            return True
        if reason and raw_exit is not None:
            await self.positions._close_position(
                session,
                pos,
                settings,
                raw_exit=raw_exit,
                reason=reason,
                exit_adverse=reason == "sl_hit",
                bar_ms=bar.time_ms,
            )
            return True
        return False

    async def monitor_session(self, session: AsyncSession) -> int:
        settings = await get_or_create_settings(session)
        result = await session.execute(
            select(SimPosition).where(SimPosition.status.in_(("pending_entry", "open")))
        )
        positions = list(result.scalars().all())
        if not positions:
            return 0
        symbol = positions[0].symbol
        limit = self._kline_limit_for_positions(positions)
        bundle = await self.market.fetch_kline_bundle(symbol, "1m", limit)
        closed = bundle.closed
        from app.services.opportunity_monitor import _utc_now

        now_ms = int(_utc_now().timestamp() * 1000)
        settings_effective = get_effective_settings()
        needs_5m = any(p.status == "pending_entry" and p.ai_setup_id for p in positions)
        closed_5m: list[Candle] = []
        five_m_data_verified = False
        five_m_verify_reason: str | None = None
        bundle_5m: KlineBundle | None = None
        if needs_5m:
            bundle_5m = await self.market.fetch_kline_bundle(symbol, "5m", 120)
            closed_5m = list(bundle_5m.closed) if bundle_5m.closed else []

        processed = 0

        for pos in positions:
            cursor = pos.last_processed_candle_ms or 0
            monitor_from = pos.monitor_from_ms or 0
            eligible = eligible_bars_after_cursor(closed, cursor, monitor_from)

            if pos.execution_state != EXECUTION_SUSPENDED and cursor > 0 and eligible:
                if leading_history_gap(cursor, eligible[0].time_ms, INTERVAL_1M_MS):
                    self._suspend_leading_gap(pos, cursor, eligible[0].time_ms, bundle)
                    continue

            if not self._try_clear_suspension(pos, bundle, closed):
                continue

            force_fill_bar_ms: int | None = None
            if (
                pos.status == "pending_entry"
                and pos.ai_setup_id
                and pos.order_type == "limit"
                and pos.limit_entry_price
            ):
                from app.models import AiProposedSetup
                from app.services.paper_auto_trading import auto_paper_entry_still_valid

                setup_row = (
                    await session.execute(
                        select(AiProposedSetup).where(AiProposedSetup.setup_id == pos.ai_setup_id)
                    )
                ).scalar_one_or_none()
                if setup_row:
                    order_ms = pos.monitor_from_ms or 0
                    if order_ms <= 0 and pos.created_at:
                        created = ensure_utc(pos.created_at)
                        if created:
                            order_ms = int(created.timestamp() * 1000)
                    if bundle_5m is not None:
                        five_m_data_verified, five_m_verify_reason = verify_five_m_for_pending_fills(
                            data_complete=bundle_5m.data_complete,
                            closed_5m=closed_5m,
                            now_ms=now_ms,
                            configured_stale_seconds=settings_effective.market_data_stale_seconds,
                            order_ms=order_ms,
                            monitor_cursor_ms=setup_row.monitor_cursor_ms,
                        )
                    if not five_m_data_verified:
                        still_ok, block_reason = await auto_paper_entry_still_valid(session, pos)
                        if not still_ok:
                            await self.positions.cancel_pending(session, pos.public_id, commit=False)
                            pos.extra = {**pos.extra, "auto_paper_cancel_reason": block_reason}
                            for bar in eligible:
                                pos.last_processed_candle_ms = bar.time_ms
                                processed += 1
                            continue
                        notes = dict(setup_row.objective_notes or {})
                        notes["pending_fill_suspended"] = True
                        notes["pending_fill_block_reason"] = (
                            five_m_verify_reason or "missing_or_incomplete_5m_data"
                        )
                        setup_row.objective_notes = notes
                        for bar in eligible:
                            pos.last_processed_candle_ms = bar.time_ms
                            processed += 1
                        continue
                    outcome = reconcile_pending_ai_limit_order(
                        order_ms=order_ms,
                        direction=setup_row.direction,
                        invalidation_level=setup_row.invalidation_level,
                        monitor_cursor_ms=setup_row.monitor_cursor_ms,
                        limit_price=float(pos.limit_entry_price or 0),
                        closed_1m=eligible,
                        closed_5m=closed_5m,
                        now_ms=now_ms,
                        five_m_data_verified=five_m_data_verified,
                        one_m_data_complete=bundle.data_complete,
                        gaps_remaining=list(bundle.gaps_remaining or []),
                    )
                    if outcome.action == "suspend":
                        notes = dict(setup_row.objective_notes or {})
                        notes["pending_fill_suspended"] = True
                        notes["pending_fill_block_reason"] = outcome.reason
                        setup_row.objective_notes = notes
                        for bar in eligible:
                            pos.last_processed_candle_ms = bar.time_ms
                            processed += 1
                        continue
                    if outcome.action == "cancel":
                        await self.positions.cancel_pending(session, pos.public_id, commit=False)
                        pos.extra = {
                            **pos.extra,
                            "auto_paper_cancel_reason": outcome.reason or "structural_invalidation",
                        }
                        from app.domain.setup_lifecycle import INVALIDATED

                        setup_row.status = "invalidated"
                        setup_row.lifecycle_state = INVALIDATED
                        for bar in eligible:
                            pos.last_processed_candle_ms = bar.time_ms
                            processed += 1
                        continue
                    if outcome.action == "fill" and outcome.fill_bar_1m is not None:
                        force_fill_bar_ms = outcome.fill_bar_1m.time_ms

            prev_ms = cursor
            for bar in eligible:
                if pos.execution_state == EXECUTION_SUSPENDED:
                    break

                missing = missing_interval_before_bar(prev_ms, bar.time_ms, INTERVAL_1M_MS)
                if missing is not None:
                    self._suspend_internal_gap(pos, prev_ms, missing, bar.time_ms, bundle)
                    break

                if self._should_block_bar_for_gaps(pos, bar, bundle):
                    self._suspend_internal_gap(
                        pos,
                        prev_ms,
                        missing or prev_ms + INTERVAL_1M_MS,
                        bar.time_ms,
                        bundle,
                    )
                    break

                entry_filled_this_bar = False

                if pos.status == "pending_entry" and pos.order_type == "limit" and pos.limit_entry_price:
                    from app.models import AiProposedSetup
                    from app.services.paper_auto_trading import auto_paper_entry_still_valid

                    if pos.ai_setup_id:
                        setup_row = (
                            await session.execute(
                                select(AiProposedSetup).where(AiProposedSetup.setup_id == pos.ai_setup_id)
                            )
                        ).scalar_one_or_none()
                        if setup_row and (setup_row.objective_notes or {}).get("pending_fill_suspended"):
                            prev_ms = bar.time_ms
                            pos.last_processed_candle_ms = bar.time_ms
                            processed += 1
                            continue
                        if setup_row and not five_m_data_verified:
                            notes = dict(setup_row.objective_notes or {})
                            notes["pending_fill_suspended"] = True
                            notes["pending_fill_block_reason"] = (
                                five_m_verify_reason or "missing_or_incomplete_5m_data"
                            )
                            setup_row.objective_notes = notes
                            prev_ms = bar.time_ms
                            pos.last_processed_candle_ms = bar.time_ms
                            processed += 1
                            continue

                    still_ok, block_reason = await auto_paper_entry_still_valid(session, pos)
                    if not still_ok:
                        await self.positions.cancel_pending(session, pos.public_id, commit=False)
                        pos.extra = {**pos.extra, "auto_paper_cancel_reason": block_reason}
                        prev_ms = bar.time_ms
                        pos.last_processed_candle_ms = bar.time_ms
                        processed += 1
                        continue
                    limit_px = pos.limit_entry_price
                    fill_this_bar = _bar_hits_limit(bar, limit_px)
                    if force_fill_bar_ms is not None and bar.time_ms == force_fill_bar_ms:
                        if not fill_this_bar:
                            notes = dict(setup_row.objective_notes or {}) if setup_row else {}
                            notes["pending_fill_suspended"] = True
                            notes["pending_fill_block_reason"] = "ambiguous_fill_vs_5m_timing"
                            if setup_row:
                                setup_row.objective_notes = notes
                            prev_ms = bar.time_ms
                            pos.last_processed_candle_ms = bar.time_ms
                            processed += 1
                            continue
                        fill_this_bar = True
                    if fill_this_bar:
                        ok_fill, _ = await auto_paper_entry_still_valid(session, pos)
                        if not ok_fill:
                            await self.positions.cancel_pending(session, pos.public_id, commit=False)
                            prev_ms = bar.time_ms
                            pos.last_processed_candle_ms = bar.time_ms
                            processed += 1
                            continue
                        await self.positions._fill_entry(
                            session, pos, settings, limit_px, transition_suffix=f"limit:{bar.time_ms}"
                        )
                        entry_filled_this_bar = True
                        pos.extra = {**pos.extra, "entry_fill_bar_ms": bar.time_ms}
                        processed += 1

                if pos.status == "open":
                    entry = pos.fill_price or pos.entry_price or pos.limit_entry_price or 0.0
                    if entry_filled_this_bar or pos.extra.get("entry_fill_bar_ms") == bar.time_ms:
                        if same_bar_exit_ambiguous_after_entry(
                            pos.direction, bar, entry, pos.stop_loss, pos.take_profit
                        ):
                            await self.positions._close_position(
                                session,
                                pos,
                                settings,
                                raw_exit=bar.close,
                                reason="ambiguous_intrabar",
                                exit_adverse=True,
                                bar_ms=bar.time_ms,
                                ambiguous=True,
                                ambiguous_policy=POLICY_SAME_BAR_ENTRY,
                            )
                            prev_ms = bar.time_ms
                            pos.last_processed_candle_ms = bar.time_ms
                            processed += 1
                            break
                    else:
                        closed_on_bar = await self._handle_open_bar(session, pos, settings, bar)
                        if closed_on_bar:
                            prev_ms = bar.time_ms
                            pos.last_processed_candle_ms = bar.time_ms
                            processed += 1
                            break

                prev_ms = bar.time_ms
                pos.last_processed_candle_ms = bar.time_ms

        await record_equity_snapshot(session, self.positions)
        if processed or positions:
            await session.commit()
        return processed
