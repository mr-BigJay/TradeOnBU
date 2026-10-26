from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.domain.tehran_time import format_tehran, tehran_iso
from app.models import SimPositionSettings, SimWalletLedger
from app.routes.auth import require_dashboard_access
from app.services.position.execution_adapters import ExecutionBackend, OrderRequest
from app.services.position.position_service import PositionServiceError, SimPositionService
from app.services.position.report_service import build_report, history_to_csv
from app.services.position.settings_service import (
    SettingsValidationError,
    get_or_create_settings,
    validate_settings_patch,
)
from app.services.position import wallet_service as wallet
from app.services.position.mark_to_market import aggregate_open_mark_to_market

router = APIRouter(prefix="/api/gui/position", tags=["position"])


class WalletDepositBody(BaseModel):
    amount_usdt: float = Field(gt=0)


class WalletInitialBody(BaseModel):
    amount_usdt: float = Field(gt=0)


class WalletWithdrawBody(BaseModel):
    amount_usdt: float = Field(gt=0)


class SettingsBody(BaseModel):
    default_margin_usdt: float | None = None
    default_leverage: int | None = None
    position_mode: str | None = None
    default_order_type: str | None = None
    default_sl_behavior: str | None = None
    default_tp_behavior: str | None = None
    fee_mode: str | None = None
    maker_fee_rate: float | None = None
    taker_fee_rate: float | None = None
    fee_source_url: str | None = None
    fee_verified_on: str | None = None
    slippage_bps: float | None = None
    max_margin_per_position: float | None = None
    max_risk_pct_per_position: float | None = None
    auto_open_paper_positions: bool | None = None


class OpenPositionBody(BaseModel):
    symbol: str = "BTCUSDT"
    direction: str
    margin_usdt: float
    leverage: int
    order_type: str = "market"
    entry_price: float | None = None
    stop_loss: float
    take_profit: float
    ai_setup_id: str | None = None


def _ledger_out(row: SimWalletLedger) -> dict:
    return {
        "id": row.id,
        "entry_type": row.entry_type,
        "amount_usdt": row.amount_usdt,
        "balance_after": row.balance_after,
        "note": row.note,
        "created_at_tehran": format_tehran(row.created_at),
        "created_at_utc": row.created_at.isoformat() if row.created_at else None,
    }


async def _mark_to_market(session: AsyncSession, svc: SimPositionService) -> dict[str, float]:
    return await aggregate_open_mark_to_market(session, svc)


@router.get("/meta")
async def meta(_=Depends(require_dashboard_access)):
    return {
        "execution_backends": [ExecutionBackend.PAPER.value, ExecutionBackend.BITUNIX_LIVE.value],
        "active_backend": ExecutionBackend.PAPER.value,
        "timezone_display": "Asia/Tehran",
    }


@router.get("/wallet")
async def wallet_view(session: AsyncSession = Depends(get_db), _=Depends(require_dashboard_access)):
    svc = SimPositionService()
    mtm = await _mark_to_market(session, svc)
    summary = await wallet.wallet_summary(
        session,
        unrealized_gross=mtm["unrealized_gross"],
        estimated_exit_fees=mtm["estimated_exit_fees"],
    )
    ledger = await wallet.list_ledger(session)
    settings = await get_or_create_settings(session)
    return {
        **summary,
        "initial_balance_set": settings.initial_balance_set,
        "ledger": [_ledger_out(r) for r in ledger],
    }


@router.post("/wallet/initial")
async def wallet_initial(
    body: WalletInitialBody,
    session: AsyncSession = Depends(get_db),
    _=Depends(require_dashboard_access),
):
    settings = await get_or_create_settings(session)
    existing = await wallet.list_ledger(session, limit=1)
    if settings.initial_balance_set or existing:
        raise HTTPException(400, "موجودی اولیه قبلاً تنظیم شده است — از واریز استفاده کنید")
    await wallet.append_ledger(
        session,
        entry_type=wallet.LEDGER_INITIAL,
        amount_usdt=body.amount_usdt,
        note="موجودی اولیه مجازی",
    )
    settings.initial_balance_set = True
    await session.commit()
    return {"ok": True}


@router.post("/wallet/deposit")
async def wallet_deposit(
    body: WalletDepositBody,
    session: AsyncSession = Depends(get_db),
    _=Depends(require_dashboard_access),
):
    await wallet.append_ledger(session, entry_type=wallet.LEDGER_DEPOSIT, amount_usdt=body.amount_usdt, note="واریز")
    await session.commit()
    return {"ok": True}


@router.post("/wallet/withdraw")
async def wallet_withdraw(
    body: WalletWithdrawBody,
    session: AsyncSession = Depends(get_db),
    _=Depends(require_dashboard_access),
):
    svc = SimPositionService()
    mtm = await _mark_to_market(session, svc)
    summary = await wallet.wallet_summary(
        session,
        unrealized_gross=mtm["unrealized_gross"],
        estimated_exit_fees=mtm["estimated_exit_fees"],
    )
    if body.amount_usdt > summary["available_balance"]:
        raise HTTPException(400, "موجودی آزاد کافی نیست")
    try:
        await wallet.append_ledger(
            session,
            entry_type=wallet.LEDGER_WITHDRAW,
            amount_usdt=-body.amount_usdt,
            note="برداشت",
        )
    except wallet.WalletError as e:
        raise HTTPException(400, str(e)) from e
    await session.commit()
    return {"ok": True}


@router.get("/settings")
async def get_settings(session: AsyncSession = Depends(get_db), _=Depends(require_dashboard_access)):
    row = await get_or_create_settings(session)
    return _settings_out(row)


def _settings_out(row: SimPositionSettings) -> dict:
    return {
        "default_margin_usdt": row.default_margin_usdt,
        "default_leverage": row.default_leverage,
        "position_mode": row.position_mode,
        "default_order_type": row.default_order_type,
        "default_sl_behavior": row.default_sl_behavior,
        "default_tp_behavior": row.default_tp_behavior,
        "fee_mode": row.fee_mode,
        "maker_fee_rate": row.maker_fee_rate,
        "taker_fee_rate": row.taker_fee_rate,
        "fee_source_url": row.fee_source_url,
        "fee_verified_on": row.fee_verified_on,
        "slippage_bps": row.slippage_bps,
        "max_margin_per_position": row.max_margin_per_position,
        "max_risk_pct_per_position": row.max_risk_pct_per_position,
        "auto_open_paper_positions": row.auto_open_paper_positions,
    }


@router.put("/settings")
async def put_settings(
    body: SettingsBody,
    session: AsyncSession = Depends(get_db),
    _=Depends(require_dashboard_access),
):
    row = await get_or_create_settings(session)
    patch = body.model_dump(exclude_unset=True)
    try:
        patch = validate_settings_patch(patch)
    except SettingsValidationError as e:
        raise HTTPException(400, str(e)) from e
    for field, val in patch.items():
        setattr(row, field, val)
    await session.commit()
    return _settings_out(row)


@router.get("/positions")
async def list_positions(session: AsyncSession = Depends(get_db), _=Depends(require_dashboard_access)):
    svc = SimPositionService()
    active = await svc.list_active(session)
    out = []
    for pos in active:
        data = await svc.enrich_open(session, pos)
        data["opened_at_tehran"] = format_tehran(pos.opened_at)
        data["created_at_tehran"] = format_tehran(pos.created_at)
        out.append(data)
    return {"positions": out}


@router.post("/positions/open")
async def open_position(
    body: OpenPositionBody,
    session: AsyncSession = Depends(get_db),
    _=Depends(require_dashboard_access),
):
    svc = SimPositionService()
    req = OrderRequest(
        symbol=body.symbol,
        direction=body.direction.upper(),
        margin_usdt=body.margin_usdt,
        leverage=body.leverage,
        order_type=body.order_type.lower(),
        entry_price=body.entry_price,
        stop_loss=body.stop_loss,
        take_profit=body.take_profit,
        ai_setup_id=body.ai_setup_id,
    )
    try:
        result = await svc.create_order(session, req)
    except PositionServiceError as e:
        raise HTTPException(400, str(e)) from e
    return result.__dict__


@router.post("/positions/{public_id}/cancel")
async def cancel_position(
    public_id: str,
    session: AsyncSession = Depends(get_db),
    _=Depends(require_dashboard_access),
):
    svc = SimPositionService()
    result = await svc.cancel_pending(session, public_id)
    if not result.success:
        raise HTTPException(400, result.message)
    return result.__dict__


@router.post("/positions/{public_id}/close")
async def close_position(
    public_id: str,
    session: AsyncSession = Depends(get_db),
    _=Depends(require_dashboard_access),
):
    svc = SimPositionService()
    result = await svc.manual_close(session, public_id)
    if not result.success:
        raise HTTPException(400, result.message)
    return result.__dict__


@router.get("/reports")
async def reports(
    period: str = Query("all"),
    start: datetime | None = None,
    end: datetime | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    session: AsyncSession = Depends(get_db),
    _=Depends(require_dashboard_access),
):
    svc = SimPositionService()
    mtm = await _mark_to_market(session, svc)
    data = await build_report(
        session,
        period=period,
        start=start,
        end=end,
        start_date=start_date,
        end_date=end_date,
    )
    data["unrealized_pnl"] = mtm["unrealized_net"]
    data["unrealized_pnl_gross"] = mtm["unrealized_gross"]
    summary = await wallet.wallet_summary(
        session,
        unrealized_gross=mtm["unrealized_gross"],
        estimated_exit_fees=mtm["estimated_exit_fees"],
    )
    data["account_equity"] = summary["account_equity"]
    return data


@router.get("/reports/export.csv")
async def export_csv(
    period: str = Query("all"),
    start: datetime | None = None,
    end: datetime | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    session: AsyncSession = Depends(get_db),
    _=Depends(require_dashboard_access),
):
    data = await build_report(
        session,
        period=period,
        start=start,
        end=end,
        start_date=start_date,
        end_date=end_date,
    )
    return PlainTextResponse(history_to_csv(data["history"]), media_type="text/csv; charset=utf-8")


@router.get('/telegram-status')
async def telegram_status(session: AsyncSession = Depends(get_db), _=Depends(require_dashboard_access)):
    import json
    import os
    import time
    from pathlib import Path
    from sqlalchemy import select
    from app.models import TelegramPaperEvent
    enabled = os.environ.get('TELEGRAM_PAPER_ENABLED') == '1'
    fresh = False
    if enabled:
        try:
            feed = json.loads((Path(os.environ.get('TELEGRAM_PAPER_INBOX', '/telegram-inbox')) / '_feed.json').read_text())
            fresh = 0 <= time.time() - float(feed['heartbeat']) <= 45
        except (OSError, ValueError, KeyError, TypeError):
            pass
    rows = (await session.scalars(select(TelegramPaperEvent).order_by(TelegramPaperEvent.created_at.desc()).limit(20))).all()
    return {'enabled': enabled, 'reader_fresh': fresh, 'events': [
        {'id':r.event_key, 'symbol':r.symbol, 'status':r.status, 'reason':r.reason, 'position_id':r.position_id}
        for r in rows]}
