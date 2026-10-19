from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.routes.auth import require_dashboard_access
from app.services.liquidation_intelligence.collector import get_liquidation_collector
from app.services.liquidation_intelligence.repository import all_source_status
from app.services.liquidation_intelligence.summary import build_liquidation_forecast_summary

router = APIRouter(prefix="/api/gui/liquidation", tags=["liquidation-intelligence"])


@router.get("/status")
async def liquidation_status(
    session: AsyncSession = Depends(get_db),
    _auth=Depends(require_dashboard_access),
):
    settings = get_settings()
    collector = get_liquidation_collector()
    sources = await all_source_status(session)
    await session.commit()
    return {
        "enabled": settings.liquidation_intelligence_enabled,
        "retention_days": {
            "raw_events": settings.liquidation_raw_retention_days,
            "rollups": settings.liquidation_rollup_retention_days,
        },
        "collector": collector.status_payload(),
        "sources": [
            {
                "exchange": s.source_exchange,
                "connected": s.connected,
                "last_message_at_ms": s.last_message_at_ms,
                "events_received": s.events_received,
                "events_rejected": s.events_rejected,
                "duplicates": s.duplicates,
                "reconnect_count": s.reconnect_count,
                "last_error": s.last_error,
            }
            for s in sources
        ],
    }


@router.get("/summary")
async def liquidation_summary_preview(
    session: AsyncSession = Depends(get_db),
    _auth=Depends(require_dashboard_access),
):
    """Read-only preview of the payload attached to forecast snapshots."""
    data = await build_liquidation_forecast_summary(session, symbol="BTCUSDT")
    await session.commit()
    return data
