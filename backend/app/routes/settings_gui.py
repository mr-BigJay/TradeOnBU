from __future__ import annotations

from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models import InstallationMeta, RuntimeSettings
from app.routes.auth import require_user
from app.services.market_data import MarketDataService
from app.services.runtime_settings import get_runtime_settings
from app.services.settings_store import (
    apply_telegram_to_env_cache,
    audit,
    get_plain,
    get_secret,
    set_plain,
    set_secret,
    telegram_public_view,
)
from app.services.telegram_notifier import TelegramNotifier
from app.services.settings_runtime import refresh_settings_cache

router = APIRouter(prefix="/api/gui/settings", tags=["gui-settings"])


class TelegramUpdate(BaseModel):
    enabled: bool = False
    bot_token: str | None = None
    bot_token_masked: str | None = None  # ignored on save
    chat_id: str | None = None
    allowed_user_ids: str = ""
    require_approval: bool = True
    use_webhook: bool = False
    webhook_secret: str | None = None


class StrategyUpdate(BaseModel):
    default_symbol: str = "BTCUSDT"
    scan_interval_seconds: int = Field(default=300, ge=60, le=3600)
    min_net_rr: float = Field(default=2.0, ge=0.5, le=20)
    max_pending_minutes: int = Field(default=90, ge=5, le=720)
    max_missed_entry_pct: float = Field(default=0.30, ge=0, le=10)
    holding_time_hours: float = Field(default=6.0, ge=0.5, le=48)
    scanning_enabled: bool = True


class RiskUpdate(BaseModel):
    margin_usdt: float = Field(default=50.0, ge=1, le=100000)
    leverage: int = Field(default=10, ge=1, le=125)
    maker_fee_rate: float = Field(default=0.0002, ge=0, le=0.01)
    taker_fee_rate: float = Field(default=0.0006, ge=0, le=0.01)
    spread_bps: float = Field(default=2.0, ge=0, le=100)
    slippage_bps: float = Field(default=5.0, ge=0, le=100)
    max_simultaneous_positions: int = Field(default=3, ge=1, le=50)
    paper_trading_paused: bool = False


@router.get("/telegram")
async def get_telegram(session: AsyncSession = Depends(get_db), _=Depends(require_user)):
    return await telegram_public_view(session)


@router.put("/telegram")
async def put_telegram(body: TelegramUpdate, session: AsyncSession = Depends(get_db), user_pair=Depends(require_user)):
    user, _ = user_pair
    await set_plain(session, "telegram", "enabled", "true" if body.enabled else "false")
    if body.bot_token:
        await set_secret(session, "telegram", "bot_token", body.bot_token.strip())
    await set_plain(session, "telegram", "chat_id", (body.chat_id or "").strip())
    await set_plain(session, "telegram", "allowed_user_ids", body.allowed_user_ids.strip())
    await set_plain(session, "telegram", "require_approval", "true" if body.require_approval else "false")
    await set_plain(session, "telegram", "use_webhook", "true" if body.use_webhook else "false")
    if body.webhook_secret:
        await set_secret(session, "telegram", "webhook_secret", body.webhook_secret.strip())
    await audit(session, user.id, "telegram", "update", {"enabled": body.enabled})
    await session.commit()
    await refresh_settings_cache(session)
    return await telegram_public_view(session)


@router.post("/telegram/test")
async def test_telegram(session: AsyncSession = Depends(get_db), user_pair=Depends(require_user)):
    await refresh_settings_cache(session)
    token = await get_secret(session, "telegram", "bot_token")
    chat = await get_plain(session, "telegram", "chat_id")
    if not token or not chat:
        return {"ok": False, "detail": "Telegram not configured"}
    url = f"https://api.telegram.org/bot{token}/getMe"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        if resp.status_code >= 400:
            return {"ok": False, "detail": resp.text}
    return {"ok": True}


@router.post("/telegram/send-test")
async def send_test_message(session: AsyncSession = Depends(get_db), user_pair=Depends(require_user)):
    await refresh_settings_cache(session)
    n = TelegramNotifier()
    if not n.enabled:
        return {"ok": False, "detail": "Telegram not configured"}
    msg_id = await n.send_message("TradeOnBU test message — paper mode only.")
    return {"ok": msg_id is not None}


@router.get("/strategy")
async def get_strategy(session: AsyncSession = Depends(get_db), _=Depends(require_user)):
    row = await get_runtime_settings(session)
    return {
        "default_symbol": row.default_symbol,
        "htf": "4H",
        "structure_tf": "15M",
        "entry_tf": "5M",
        "refinement_tf": "1M",
        "scan_interval_seconds": row.scan_interval_seconds,
        "min_net_rr": row.min_net_rr,
        "max_pending_minutes": row.max_pending_minutes,
        "max_missed_entry_pct": row.max_missed_entry_pct,
        "holding_time_hours": row.holding_time_hours,
        "scanning_enabled": row.scanning_enabled,
    }


@router.put("/strategy")
async def put_strategy(body: StrategyUpdate, session: AsyncSession = Depends(get_db), user_pair=Depends(require_user)):
    user, _ = user_pair
    row = await get_runtime_settings(session)
    row.default_symbol = body.default_symbol
    row.scan_interval_seconds = body.scan_interval_seconds
    row.min_net_rr = body.min_net_rr
    row.max_pending_minutes = body.max_pending_minutes
    row.max_missed_entry_pct = body.max_missed_entry_pct
    row.holding_time_hours = body.holding_time_hours
    row.scanning_enabled = body.scanning_enabled
    await audit(session, user.id, "strategy", "update", body.model_dump())
    await session.commit()
    await refresh_settings_cache(session)
    return await get_strategy(session)


@router.get("/risk")
async def get_risk(session: AsyncSession = Depends(get_db), _=Depends(require_user)):
    row = await get_runtime_settings(session)
    return {
        "margin_usdt": row.margin_usdt,
        "leverage": row.leverage,
        "position_notional_usdt": row.margin_usdt * row.leverage,
        "trading_mode": "PAPER_ONLY",
        "min_net_rr": row.min_net_rr,
        "maker_fee_rate": row.maker_fee_rate,
        "taker_fee_rate": row.taker_fee_rate,
        "spread_bps": row.spread_bps,
        "slippage_bps": row.slippage_bps,
        "max_simultaneous_positions": row.max_simultaneous_positions,
        "paper_trading_paused": row.paper_trading_paused,
        "leverage_disclaimer": "Simulation only — no Bitunix leverage is changed.",
    }


@router.put("/risk")
async def put_risk(body: RiskUpdate, session: AsyncSession = Depends(get_db), user_pair=Depends(require_user)):
    user, _ = user_pair
    row = await get_runtime_settings(session)
    row.margin_usdt = body.margin_usdt
    row.leverage = body.leverage
    row.maker_fee_rate = body.maker_fee_rate
    row.taker_fee_rate = body.taker_fee_rate
    row.spread_bps = body.spread_bps
    row.slippage_bps = body.slippage_bps
    row.max_simultaneous_positions = body.max_simultaneous_positions
    row.paper_trading_paused = body.paper_trading_paused
    await audit(session, user.id, "risk", "update", body.model_dump())
    await session.commit()
    await refresh_settings_cache(session)
    return await get_risk(session)


@router.get("/market")
async def market_status(session: AsyncSession = Depends(get_db), _=Depends(require_user)):
    settings = get_settings()
    row = await get_runtime_settings(session)
    md = MarketDataService(settings)
    try:
        quote = await md.fetch_live_price(row.default_symbol)
        stale = md.is_quote_stale(quote)
        last_at = quote.fetched_at.isoformat()
        ticker_ok = True
    except Exception as exc:
        stale = True
        last_at = None
        ticker_ok = False
        quote = None
        err = str(exc)
    else:
        err = None
    return {
        "exchange": "Bitunix Futures",
        "symbol": row.default_symbol,
        "public_api_status": "ok" if ticker_ok else "error",
        "last_market_update": last_at,
        "data_fresh": not stale,
        "ticker_available": ticker_ok,
        "last_error": err,
        "live_price": quote.price if quote else None,
    }


@router.get("/system")
async def system_status(session: AsyncSession = Depends(get_db), _=Depends(require_user)):
    meta = (await session.execute(__import__("sqlalchemy").select(InstallationMeta).where(InstallationMeta.id == 1))).scalar_one_or_none()
    return {
        "domain": meta.domain if meta else None,
        "https_expected": True,
        "paper_only": True,
        "live_trading": False,
        "installed_at": meta.installed_at.isoformat() if meta and meta.installed_at else None,
    }
