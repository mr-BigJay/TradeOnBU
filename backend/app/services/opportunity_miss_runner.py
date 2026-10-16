"""Run replay + diff for setups (offline / dashboard audit)."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.opportunity_miss_reasons import REPLAY_STATUS_OK
from app.models import AiProposedSetup
from app.services.confirmation_replay import (
    assess_replay_history,
    detect_same_5m_multi_token_candidates,
    diff_live_vs_replay,
    replay_confirmations_on_5m_bars,
)
from app.services.market_data import MarketDataService
from app.services.monitor_kline_fetch import MONITOR_INTERVAL, fetch_monitor_5m_bundle
from app.services.opportunity_miss_report import build_miss_audit_report
from app.services.opportunity_monitor import _monitor_boundary_ms, _zone_bounds


async def replay_open_setups(
    session: AsyncSession,
    *,
    symbol: str,
    market: MarketDataService | None = None,
) -> dict[str, Any]:
    market = market or MarketDataService()
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
    bundle_5m, fetch_meta = await fetch_monitor_5m_bundle(market, symbol, setups, now_ms=now_ms)
    bundle_1m = await market.fetch_historical_closed(
        symbol,
        "1m",
        from_ms=max(0, (fetch_meta.get("from_ms") or now_ms - 3_600_000)),
        max_bars=min(2400, max(500, len(bundle_5m.closed) * 5 + 50)),
    )

    replay_by_setup: dict[str, dict[str, Any]] = {}
    diffs: list[dict[str, Any]] = []

    for setup in setups:
        conf = setup.confirmation_state or {}
        seq = conf.get("sequence") or conf.get("required") or []
        if not seq or setup.invalidation_level is None:
            continue
        boundary = _monitor_boundary_ms(setup)
        zone_lo, zone_hi = _zone_bounds(setup)
        replay_status, status_notes = assess_replay_history(
            closed_bars=bundle_5m.closed,
            monitor_from_ms=boundary,
            monitor_cursor_ms=setup.monitor_cursor_ms,
            now_ms=now_ms,
            data_complete=bundle_5m.data_complete,
            gaps_remaining=list(bundle_5m.gaps_remaining or []),
            objective_notes=setup.objective_notes or {},
        )
        multi = detect_same_5m_multi_token_candidates(
            direction=setup.direction,
            sequence=[str(x) for x in seq],
            closed_5m=bundle_5m.closed,
            closed_1m=bundle_1m.closed,
            zone_lo=zone_lo,
            zone_hi=zone_hi,
            structured=setup.structured_snapshot or {},
            monitor_from_ms=boundary,
            now_ms=now_ms,
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
        )
        diff = diff_live_vs_replay(
            setup_id=setup.setup_id,
            live_state=conf,
            replay=replay,
            multi_candidates=multi,
            replay_status=replay_status,
            replay_status_notes=status_notes,
        )
        payload = {
            "replay_status": diff.replay_status,
            "replay_status_notes": diff.replay_status_notes,
            "missing_in_live": diff.missing_in_live,
            "extra_in_live": diff.extra_in_live,
            "order_mismatch": diff.order_mismatch,
            "live_timestamps": diff.live_timestamps,
            "replay_timestamps": diff.replay_timestamps,
            "same_bar_multi_token_candidates": diff.same_bar_multi_token_candidates,
            "notes": diff.notes,
            "replay_trusted": diff.replay_status == REPLAY_STATUS_OK,
        }
        replay_by_setup[setup.setup_id] = payload
        diffs.append({"setup_id": setup.setup_id, **payload})

    report = await build_miss_audit_report(session, symbol=symbol, limit=200, replay_by_setup=replay_by_setup)
    return {
        "fetch_meta": fetch_meta,
        "kline_5m_limit": fetch_meta.get("aggregate_limit"),
        "data_complete_5m": bundle_5m.data_complete,
        "gaps_remaining_5m": bundle_5m.gaps_remaining,
        "data_complete_1m": bundle_1m.data_complete,
        "replay_diffs": diffs,
        "report": report,
    }
