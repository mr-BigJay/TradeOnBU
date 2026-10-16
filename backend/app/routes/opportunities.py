from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
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
from app.services import opportunity_list as opp_list
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
    "net_rr_below_min_at_execution_price": "R:R خالص در قیمت اجرای واقعی زیر حد مجاز",
    "entry_too_far_from_live": "فاصله قیمت از POI برای ورود منطقی نیست (بدون تعقیب)",
    "max_concurrent_auto_paper": "فقط یک سفارش/پوزیشن خودکار هم‌زمان مجاز است",
    "daily_auto_paper_cap_tehran": "سقف دو سفارش خودکار در روز (تهران) پر شده",
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


def _serialize_position(row: SimPosition, setup: AiProposedSetup | None, live_price: float | None) -> dict:
    base = {
        "entity_type": "paper_position",
        "public_id": row.public_id,
        "symbol": row.symbol,
        "direction": row.direction,
        "status": row.status,
        "paper_segment": (
            "pending"
            if row.status == "pending_entry"
            else "open"
            if row.status == "open"
            else "closed"
        ),
        "limit_entry_price": row.limit_entry_price,
        "fill_price": row.fill_price,
        "stop_loss": row.stop_loss,
        "take_profit": row.take_profit,
        "ai_setup_id": row.ai_setup_id,
        "created_at_tehran": format_tehran(row.created_at),
        "updated_at_tehran": format_tehran(row.updated_at),
        "status_label_fa": _PERSIAN_STATUS.get(
            PENDING_ENTRY if row.status == "pending_entry" else POSITION_OPEN if row.status == "open" else POSITION_CLOSED,
            row.status,
        ),
    }
    if setup:
        base["setup"] = _serialize_setup(setup, live_price)
    return base


def _serialize_no_trade_run(row: AiAnalysisRun) -> dict:
    return {
        "entity_type": "no_trade",
        "run_id": row.id,
        "symbol": row.symbol,
        "decision": row.decision,
        "final_ai_decision": row.final_ai_decision,
        "created_at_tehran": format_tehran(row.created_at),
        "preview": (row.report_fa or "")[:400],
        "report_fa": row.report_fa,
    }


@router.get("/summary")
async def opportunities_summary(session: AsyncSession = Depends(get_db), _=Depends(require_dashboard_access)):
    """Real counts for dashboard summary cards (server-side, not limited to 200 rows)."""
    counts = await opp_list.fetch_summary_counts(session)
    from app.services.paper_execution_metrics import build_execution_metrics

    exec_kpi = await build_execution_metrics(session, period="30d")
    return {
        "counts": counts,
        "execution_kpi": {
            "executed_auto_paper": exec_kpi.get("executed_auto_paper"),
            "monitoring": {
                "ready_or_beyond_candidates": exec_kpi.get("monitoring", {}).get("ready_or_beyond_candidates"),
                "skip_reason_counts": exec_kpi.get("monitoring", {}).get("skip_reason_counts"),
            },
            "data_quality_note": exec_kpi.get("data_quality_note"),
        },
    }


@router.get("/execution-metrics")
async def execution_metrics(
    session: AsyncSession = Depends(get_db),
    _=Depends(require_dashboard_access),
    period: str = Query("30d", description="today|7d|30d"),
):
    from app.services.paper_execution_metrics import build_execution_metrics

    if period not in ("today", "7d", "30d"):
        raise HTTPException(status_code=400, detail="invalid period")
    return await build_execution_metrics(session, period=period)


@router.get("/execution-backtest")
async def execution_backtest_compare(
    session: AsyncSession = Depends(get_db),
    _=Depends(require_dashboard_access),
    symbol: str = "BTCUSDT",
    limit: int = Query(300, ge=10, le=2000),
):
    from app.services.paper_execution_backtest import compare_entry_policies_on_history

    return await compare_entry_policies_on_history(session, symbol=symbol, limit=limit)


@router.get("/items")
async def opportunities_items(
    session: AsyncSession = Depends(get_db),
    _=Depends(require_dashboard_access),
    tab: str = Query(..., description="wait|ready|blocked|invalidated|expired|paper|no_trade|other"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=20),
    paper_status: str = Query(
        "pending",
        description="When tab=paper: pending|open|closed",
    ),
):
    tab_norm = tab.strip().lower()
    if tab_norm not in opp_list.VALID_TABS:
        raise HTTPException(status_code=400, detail=f"invalid tab: {tab}")
    if tab_norm == opp_list.TAB_PAPER and paper_status not in opp_list.VALID_PAPER_STATUS:
        raise HTTPException(status_code=400, detail=f"invalid paper_status: {paper_status}")

    from app.services.market_data import MarketDataService

    market = MarketDataService()
    try:
        live, _ = await market.fetch_ticker("BTCUSDT")
    except Exception:
        live = None

    rows, meta = await opp_list.list_tab_items(
        session,
        tab=tab_norm,
        page=page,
        page_size=page_size,
        paper_status=paper_status,
    )

    items: list[dict] = []
    if tab_norm == opp_list.TAB_PAPER:
        setup_ids = [r.ai_setup_id for r in rows if r.ai_setup_id]
        setups_by_id: dict[str, AiProposedSetup] = {}
        if setup_ids:
            for s in (
                await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id.in_(setup_ids)))
            ).scalars():
                setups_by_id[s.setup_id] = s
        for pos in rows:
            setup = setups_by_id.get(pos.ai_setup_id) if pos.ai_setup_id else None
            items.append(_serialize_position(pos, setup, live))
    elif tab_norm == opp_list.TAB_NO_TRADE:
        items = [_serialize_no_trade_run(r) for r in rows]
    else:
        items = [{**_serialize_setup(s, live), "entity_type": "setup"} for s in rows]

    return {
        "tab": tab_norm,
        "paper_status": paper_status if tab_norm == opp_list.TAB_PAPER else None,
        "items": items,
        "total": meta["total"],
        "page": meta["page"],
        "page_size": meta["page_size"],
        "total_pages": meta["total_pages"],
        "live_price": live,
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
    ready = [s for s in setups if s.lifecycle_state in (READY_LONG, READY_SHORT) and s.status == "open"]
    ict_confirmed = [
        s
        for s in setups
        if s.lifecycle_state in (ICT_CONFIRMED_LONG, ICT_CONFIRMED_SHORT) and s.status == "open"
    ]
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
        "ict_confirmed_setups": [_serialize_setup(s, live) for s in ict_confirmed],
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


@router.get("/miss-audit")
async def miss_audit_report(
    session: AsyncSession = Depends(get_db),
    _=Depends(require_dashboard_access),
    symbol: str = "BTCUSDT",
    include_replay: bool = True,
):
    """Opportunity miss audit: timelines, outcome buckets, optional live vs replay diff."""
    from app.services.opportunity_miss_report import build_miss_audit_report
    from app.services.opportunity_miss_runner import replay_open_setups

    replay_by_setup: dict = {}
    replay_meta: dict = {}
    if include_replay:
        replay_out = await replay_open_setups(session, symbol=symbol)
        replay_meta = {
            "fetch_meta": replay_out.get("fetch_meta"),
            "kline_5m_limit": replay_out.get("kline_5m_limit"),
            "data_complete_5m": replay_out.get("data_complete_5m"),
            "gaps_remaining_5m": replay_out.get("gaps_remaining_5m"),
            "data_complete_1m": replay_out.get("data_complete_1m"),
        }
        for row in replay_out.get("replay_diffs") or []:
            sid = row.get("setup_id")
            if sid:
                replay_by_setup[sid] = row

    report = await build_miss_audit_report(session, symbol=symbol, limit=200, replay_by_setup=replay_by_setup)
    return {"replay_meta": replay_meta, **report}
