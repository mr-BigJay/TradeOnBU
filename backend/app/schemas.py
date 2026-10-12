from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RuntimeSettingsUpdate(BaseModel):
    max_pending_minutes: int | None = Field(None, ge=1, le=24 * 60)
    max_missed_entry_pct: float | None = Field(None, ge=0.01, le=5.0)
    holding_time_hours: float | None = Field(None, ge=0.5, le=72.0)
    min_net_rr: float | None = Field(None, ge=1.0, le=10.0)


class RuntimeSettingsOut(RuntimeSettingsUpdate):
    max_pending_minutes: int
    max_missed_entry_pct: float
    holding_time_hours: float
    min_net_rr: float
    updated_at: datetime | None = None


class TrendSnapshotOut(BaseModel):
    symbol: str
    trend_4h: str
    trend_15m: str
    trend_5m: str
    ict_bias: str
    details: dict[str, Any]
    recorded_at: datetime
    live_price: float | None = None
    price_stale: bool = False
    last_price_at: datetime | None = None


class SignalOut(BaseModel):
    id: int
    public_id: str
    symbol: str
    source: str
    status: str
    direction: str
    entry_type: str
    entry_price: float
    entry_zone_low: float | None
    entry_zone_high: float | None
    stop_loss: float
    take_profit: float
    invalidation_level: float
    market_bias: str
    liquidity_sweep: bool
    mss_confirmation: bool
    fvg_entry: bool
    gross_rr: float
    net_rr: float
    estimated_holding_hours: float
    analysis: dict[str, Any]
    pending_expires_at: datetime | None
    active_expires_at: datetime | None
    activated_at: datetime | None
    closed_at: datetime | None
    fill_price: float | None
    exit_price: float | None
    exit_reason: str | None
    estimated_net_pnl_usdt: float | None
    telegram_approval_required: bool
    telegram_approved: bool | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SignalEventOut(BaseModel):
    id: int
    signal_id: int
    event_type: str
    from_status: str | None
    to_status: str | None
    payload: dict[str, Any]
    note: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatGPTImportRequest(BaseModel):
    symbol: str = "BTCUSDT"
    direction: str = Field(..., pattern="^(LONG|SHORT)$")
    entry_type: str = Field("LIMIT", pattern="^(LIMIT|STOP|CONFIRMATION)$")
    entry_price: float
    entry_zone_low: float | None = None
    entry_zone_high: float | None = None
    stop_loss: float
    take_profit: float
    invalidation_level: float | None = None
    market_bias: str | None = None
    liquidity_sweep: bool = False
    mss_confirmation: bool = False
    fvg_entry: bool = False
    notes: str | None = None


class TelegramApprovalRequest(BaseModel):
    public_id: str
    approved: bool
