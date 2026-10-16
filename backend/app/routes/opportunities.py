from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.domain.setup_lifecycle import (
    EXPIRED,
    INVALIDATED,
    PENDING_ENTRY,
    POSITION_CLOSED,
    POSITION_OPEN,
    DISPLAY_ICT_CONFIRMED_BLOCKED_RR,
    ICT_CONFIRMED_LONG,
    ICT_CONFIRMED_SHORT,
    READY_LONG,
    READY_SHORT,
    REASON_AUTO_PAPER_DISABLED,
    REASON_ICT_CONFIRMED_BLOCKED_RR,
    WAIT_LONG,
    WAIT_SHORT,
)
from app.models import AiAnalysisRun, AiProposedSetup, SimPosition
from app.routes.auth import require_dashboard_access
from app.services.opportunity_audit import aggregate_opportunity_stats
from app.domain.tehran_time import format_tehran

router = APIRouter(prefix="/api/gui/opportunities", tags=["opportunities"])

_PERSIAN_STATUS: dict[str, str] = {
    WAIT_LONG: "در انتظار تأیید — LONG",
    WAIT_SHORT: "در انتظار تأیید — SHORT",
    READY_LONG: "آماده اجرای Paper — LONG",
    READY_SHORT: "آماده اجرای Paper — SHORT",
    ICT_CONFIRMED_LONG: "تأیید ICT کامل — R:R خالص ناکافی (LONG)",
    ICT_CONFIRMED_SHORT: "تأیید ICT کامل — R:R خالص ناکافی (SHORT)",
    PENDING_ENTRY: "سفارش Paper ثبت شد؛ در انتظار برخورد قیمت",
    POSITION_OPEN: "پوزیشن Paper باز",
    POSITION_CLOSED: "پوزیشن بسته شد",
    INVALIDATED: "سناریو به دلیل شکست ساختار باطل شد",
    EXPIRED: "سناریو منقضی شد",
}

_REJECTION_FA: dict[str, str] = {
    "missing_confirmations": "در انتظار تأییدهای ICT (MSS، sweep، POI و …)",
    "net_rr_below_min": "نسبت ریسک به بازده خالص کافی نیست (حداقل ۱:۲)",
    "ict_confirmed_blocked_by_net_rr": DISPLAY_ICT_CONFIRMED_BLOCKED_RR,
    "auto_paper_disabled": "ورود خودکار Paper غیرفعال است",
    "wallet_not_initialized": "موجودی Paper اولیه تنظیم نشده",
    "missing_market_data": "داده بازار ناقص — اجرا متوقف",
    "stale_market_data": "داده بازار کهنه — اجرا متوقف",
    "structural_invalidation": "سناریو به دلیل شکست ساختار باطل شد",
    "setup_expired": "زمان اعتبار سناریو تمام شد",
}


def _age_minutes(created: datetime | None) -> int | None:
    if not created:
        return None
    dt = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - dt).total_seconds() // 60)


def _serialize_setup(row: AiProposedSetup, live_price: float | None = None) -> dict:
    conf = row.confirmation_state or {}
    return {
        "setup_id": row.setup_id,
        "symbol": row.symbol,
        "direction": row.direction,
        "lifecycle_state": row.lifecycle_state,
        "status_label_fa": _PERSIAN_STATUS.get(row.lifecycle_state, row.lifecycle_state),
        "setup_mode": row.setup_mode,
        "final_ai_decision": row.final_ai_decision,
        "entry_zone_low": row.entry_zone_low,
        "entry_zone_high": row.entry_zone_high,
        "entry_price": row.entry_price,
        "stop_loss": row.stop_loss,
        "take_profit": row.take_profit,
        "net_rr": (row.last_validation_result or {}).get("net_rr"),
        "live_price": live_price,
        "created_at_tehran": format_tehran(row.created_at),
        "expires_at_tehran": format_tehran(row.expires_at),
        "age_minutes": _age_minutes(row.created_at),
        "required_confirmations": conf.get("required") or [],
        "completed_confirmations": conf.get("completed") or [],
        "missing_confirmations": conf.get("missing") or [],
        "last_monitor_at_tehran": format_tehran(row.last_monitor_at),
        "rejection_reason": row.last_rejection_reason,
        "rejection_label_fa": _REJECTION_FA.get(row.last_rejection_reason or "", row.last_rejection_reason),
        "display_status": (row.last_validation_result or {}).get("display_status"),
        "ict_confirmed": (row.last_validation_result or {}).get("ict_confirmed"),
        "order_block_reason": (row.last_validation_result or {}).get("order_skipped_reason"),
        "revision_history": (row.objective_notes or {}).get("revision_history") or [],
        "lifecycle_history": (row.objective_notes or {}).get("lifecycle_history") or [],
        "run_id": row.run_id,
    }


@router.get("/dashboard")
async def dashboard(session: AsyncSession = Depends(get_db), _=Depends(require_dashboard_access)):
    from app.services.market_data import MarketDataService

    market = MarketDataService()
    live, _ = await market.fetch_ticker("BTCUSDT")

    setups = list(
        (
            await session.execute(select(AiProposedSetup).order_by(desc(AiProposedSetup.updated_at)).limit(200))
        ).scalars()
    )
    wait = [s for s in setups if s.lifecycle_state in (WAIT_LONG, WAIT_SHORT) and s.status == "open"]
    ready = [s for s in setups if s.lifecycle_state in (READY_LONG, READY_SHORT)]
    invalidated = [s for s in setups if s.lifecycle_state == INVALIDATED or s.status == "invalidated"]
    expired = [s for s in setups if s.lifecycle_state == EXPIRED or s.status == "expired"]

    positions = list((await session.execute(select(SimPosition).order_by(desc(SimPosition.updated_at)).limit(100))).scalars())
    pending = [p for p in positions if p.status == "pending_entry"]
    open_pos = [p for p in positions if p.status == "open"]

    recent_runs = list(
        (
            await session.execute(select(AiAnalysisRun).order_by(desc(AiAnalysisRun.id)).limit(30))
        ).scalars()
    )
    no_trade_reports = [
        r
        for r in recent_runs
        if (r.decision or "").upper() == "NO_TRADE"
        and (r.final_ai_decision or "").upper() not in ("WAIT",)
        and (r.setup_mode or "") != "conditional"
    ]

    stats = await aggregate_opportunity_stats(session)

    return {
        "live_price": live,
        "wait_setups": [_serialize_setup(s, live) for s in wait],
        "ready_setups": [_serialize_setup(s, live) for s in ready],
        "pending_orders": [
            {
                "public_id": p.public_id,
                "direction": p.direction,
                "status": p.status,
                "limit_entry_price": p.limit_entry_price,
                "ai_setup_id": p.ai_setup_id,
                "status_label_fa": _PERSIAN_STATUS.get(PENDING_ENTRY),
            }
            for p in pending
        ],
        "open_positions": [
            {
                "public_id": p.public_id,
                "direction": p.direction,
                "status": p.status,
                "fill_price": p.fill_price,
                "ai_setup_id": p.ai_setup_id,
                "status_label_fa": _PERSIAN_STATUS.get(POSITION_OPEN),
            }
            for p in open_pos
        ],
        "invalidated_setups": [_serialize_setup(s, live) for s in invalidated[:50]],
        "expired_setups": [_serialize_setup(s, live) for s in expired[:50]],
        "no_trade_reports": [
            {
                "run_id": r.id,
                "created_at_tehran": format_tehran(r.created_at),
                "decision": r.decision,
                "final_ai_decision": r.final_ai_decision,
                "preview": (r.report_fa or "")[:240],
            }
            for r in no_trade_reports[:20]
        ],
        "stats": stats,
    }
