from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.liquidation_intelligence.coverage import append_coverage_gap
from app.services.liquidation_intelligence.repository import all_source_status


async def reconcile_liquidation_sources_on_startup(session: AsyncSession) -> int:
    """Close unverified DB 'connected' rows after unclean shutdown — never credit downtime as coverage."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    changed = 0
    for st in await all_source_status(session):
        if not st.connected:
            continue
        verified_end = int(st.last_connection_activity_at_ms or st.last_heartbeat_at_ms or 0)
        start = int(st.connected_since_ms or 0)
        if start > 0 and verified_end > start:
            st.coverage_gaps = append_coverage_gap(
                st.coverage_gaps,
                start_ms=start,
                end_ms=verified_end,
                now_ms=now_ms,
            )
        st.connected = False
        st.connected_since_ms = None
        st.subscription_confirmed = False
        st.last_error = (st.last_error or "startup_reconcile_unclean_disconnect")[:255]
        changed += 1
    return changed
