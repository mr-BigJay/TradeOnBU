"""Build per-setup ICT opportunity timelines for miss audit."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.candles import Candle
from app.domain.candle_utils import candle_close_ms
from app.models import AiAnalysisRun, AiProposedSetup, IctSetupAuditEvent, SimPosition


def _iso(ms: int | None) -> str | None:
    if ms is None or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _iso_dt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def append_timeline_event(setup: AiProposedSetup, event: str, **fields: Any) -> None:
    notes = dict(setup.objective_notes or {})
    timeline = list(notes.get("opportunity_timeline") or [])
    timeline.append(
        {
            "event": event,
            "at": datetime.now(timezone.utc).isoformat(),
            **fields,
        }
    )
    notes["opportunity_timeline"] = timeline[-200:]
    setup.objective_notes = notes


async def build_setup_timeline(session: AsyncSession, setup: AiProposedSetup) -> dict[str, Any]:
    conf = setup.confirmation_state or {}
    ts_map = conf.get("confirmation_timestamps") or {}
    sequence = conf.get("sequence") or conf.get("required") or []

    confirmation_events = []
    for tok in sequence:
        ms = ts_map.get(tok)
        if ms is not None:
            confirmation_events.append(
                {
                    "token": tok,
                    "bar_open_ms": int(ms),
                    "bar_close_ms": candle_close_ms(Candle(time_ms=int(ms), open=0, high=0, low=0, close=0, volume=0), "5m"),
                    "at_tehran_hint": _iso(int(ms)),
                }
            )

    audit_rows = list(
        (
            await session.execute(
                select(IctSetupAuditEvent)
                .where(IctSetupAuditEvent.setup_id == setup.setup_id)
                .order_by(IctSetupAuditEvent.created_at)
            )
        ).scalars()
    )
    audit = [
        {
            "event_type": r.event_type,
            "reason_code": r.reason_code,
            "created_at": _iso_dt(r.created_at),
            "payload": r.payload or {},
        }
        for r in audit_rows
    ]

    positions = list(
        (
            await session.execute(
                select(SimPosition).where(SimPosition.ai_setup_id == setup.setup_id).order_by(SimPosition.created_at)
            )
        ).scalars()
    )
    orders = [
        {
            "public_id": p.public_id,
            "status": p.status,
            "created_at": _iso_dt(p.created_at),
            "fill_price": p.fill_price,
            "limit_entry_price": p.limit_entry_price,
        }
        for p in positions
    ]

    run_created: str | None = None
    if setup.run_id:
        run = (
            await session.execute(select(AiAnalysisRun).where(AiAnalysisRun.id == setup.run_id))
        ).scalar_one_or_none()
        if run:
            run_created = _iso_dt(run.created_at or run.started_at)

    notes_timeline = (setup.objective_notes or {}).get("opportunity_timeline") or []
    last_val = setup.last_validation_result or {}

    return {
        "setup_id": setup.setup_id,
        "symbol": setup.symbol,
        "direction": setup.direction,
        "lifecycle_state": setup.lifecycle_state,
        "chatgpt_analysis_created_at": run_created,
        "setup_created_at": _iso_dt(setup.created_at),
        "monitor_from_ms": setup.monitor_from_ms,
        "monitor_cursor_ms": setup.monitor_cursor_ms,
        "last_monitor_at": _iso_dt(setup.last_monitor_at),
        "confirmation_events": confirmation_events,
        "final_confirmation_bar_close_ms": (
            max((e["bar_close_ms"] for e in confirmation_events), default=None) if confirmation_events else None
        ),
        "ready_at": last_val.get("promoted_at") or last_val.get("ready_at"),
        "order_created_at": last_val.get("order_created_at"),
        "order_skipped_reason": last_val.get("order_skipped_reason"),
        "rejection_reason": setup.last_rejection_reason,
        "audit_events": audit,
        "paper_orders": orders,
        "notes_timeline": notes_timeline,
    }
