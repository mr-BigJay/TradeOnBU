"""Run replay + diff for setups (offline / dashboard audit)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiProposedSetup
from app.services.confirmation_replay import (
    detect_same_5m_multi_token_candidates,
    diff_live_vs_replay,
    replay_confirmations_on_5m_bars,
)
from app.services.market_data import MarketDataService
from app.services.monitor_kline_backfill import aggregate_monitor_kline_limit
from app.services.opportunity_miss_report import build_miss_audit_report
from app.services.opportunity_monitor import MONITOR_INTERVAL, _monitor_boundary_ms, _zone_bounds


async def replay_open_setups(
    session: AsyncSession,
    *,
    symbol: str,
    market: MarketDataService | None = None,
) -> dict[str, Any]:
    market = market or MarketDataService()
    import time

    now_ms = int(time.time() * 1000)
    setups = list(
        (
            await session.execute(
                select(AiProposedSetup)
                .where(AiProposedSetup.symbol == symbol)
                .where(AiProposedSetup.status == "open")
                .where(AiProposedSetup.monitoring_enabled.is_(True))
            )
        ).scalars()
    )
    limit, _ = aggregate_monitor_kline_limit(setups, now_ms=now_ms, interval=MONITOR_INTERVAL)
    bundle_5m = await market.fetch_kline_bundle(symbol, MONITOR_INTERVAL, limit)
    bundle_1m = await market.fetch_kline_bundle(symbol, "1m", min(500, limit * 5))

    replay_by_setup: dict[str, dict[str, Any]] = {}
    diffs: list[dict[str, Any]] = []

    for setup in setups:
        conf = setup.confirmation_state or {}
        seq = conf.get("sequence") or conf.get("required") or []
        if not seq or setup.invalidation_level is None:
            continue
        boundary = _monitor_boundary_ms(setup)
        zone_lo, zone_hi = _zone_bounds(setup)
        multi = detect_same_5m_multi_token_candidates(
            direction=setup.direction,
            sequence=[str(x) for x in seq],
            closed_5m=bundle_5m.closed,
            closed_1m=bundle_1m.closed,
            zone_lo=zone_lo,
            zone_hi=zone_hi,
            structured=setup.structured_snapshot or {},
            monitor_from_ms=boundary,
        )
        replay = replay_confirmations_on_5m_bars(
            direction=setup.direction,
            sequence=[str(x) for x in seq],
            closed_bars=bundle_5m.closed,
            zone_lo=zone_lo,
            zone_hi=zone_hi,
            invalidation_level=setup.invalidation_level,
            structured=setup.structured_snapshot or {},
            monitor_from_ms=boundary,
            start_cursor_ms=setup.monitor_cursor_ms,
        )
        diff = diff_live_vs_replay(
            setup_id=setup.setup_id,
            live_state=conf,
            replay=replay,
            multi_candidates=multi,
        )
        payload = {
            "missing_in_live": diff.missing_in_live,
            "extra_in_live": diff.extra_in_live,
            "order_mismatch": diff.order_mismatch,
            "live_timestamps": diff.live_timestamps,
            "replay_timestamps": diff.replay_timestamps,
            "same_bar_multi_token_candidates": diff.same_bar_multi_token_candidates,
            "notes": diff.notes,
        }
        replay_by_setup[setup.setup_id] = payload
        diffs.append({"setup_id": setup.setup_id, **payload})

    report = await build_miss_audit_report(session, symbol=symbol, limit=200, replay_by_setup=replay_by_setup)
    return {
        "kline_5m_limit": limit,
        "data_complete_5m": bundle_5m.data_complete,
        "gaps_remaining_5m": bundle_5m.gaps_remaining,
        "replay_diffs": diffs,
        "report": report,
    }
