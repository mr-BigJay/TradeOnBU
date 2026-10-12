import enum
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TrendState(str, enum.Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    SIDEWAYS = "SIDEWAYS"
    UNCERTAIN = "UNCERTAIN"


class SignalStatus(str, enum.Enum):
    CREATED = "CREATED"
    WAITING_FOR_ENTRY = "WAITING_FOR_ENTRY"
    ACTIVE = "ACTIVE"
    TP_HIT = "TP_HIT"
    SL_HIT = "SL_HIT"
    TIME_EXIT = "TIME_EXIT"
    MISSED_ENTRY = "MISSED_ENTRY"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
    REJECTED = "REJECTED"
    DATA_ERROR = "DATA_ERROR"


class EntryType(str, enum.Enum):
    LIMIT = "LIMIT"
    STOP = "STOP"
    CONFIRMATION = "CONFIRMATION"


class SignalSource(str, enum.Enum):
    RULE_BASED = "RULE_BASED"
    CHATGPT_IMPORT = "CHATGPT_IMPORT"


class SystemState(Base):
    __tablename__ = "system_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    telegram_update_offset: Mapped[int] = mapped_column(Integer, default=0)
    worker_lock_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    worker_lock_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RuntimeSettings(Base):
    __tablename__ = "runtime_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    max_pending_minutes: Mapped[int] = mapped_column(Integer, default=90)
    max_missed_entry_pct: Mapped[float] = mapped_column(Float, default=0.30)
    holding_time_hours: Mapped[float] = mapped_column(Float, default=6.0)
    min_net_rr: Mapped[float] = mapped_column(Float, default=2.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class MarketTrendSnapshot(Base):
    __tablename__ = "market_trend_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    trend_4h: Mapped[str] = mapped_column(String(16))
    trend_15m: Mapped[str] = mapped_column(String(16))
    trend_5m: Mapped[str] = mapped_column(String(16))
    ict_bias: Mapped[str] = mapped_column(String(16))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(32), default=SignalSource.RULE_BASED.value)
    status: Mapped[str] = mapped_column(String(32), index=True, default=SignalStatus.CREATED.value)

    direction: Mapped[str] = mapped_column(String(8))
    entry_type: Mapped[str] = mapped_column(String(16), default=EntryType.LIMIT.value)
    entry_price: Mapped[float] = mapped_column(Float)
    entry_zone_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_zone_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    invalidation_level: Mapped[float] = mapped_column(Float)

    market_bias: Mapped[str] = mapped_column(String(16))
    liquidity_sweep: Mapped[bool] = mapped_column(default=False)
    mss_confirmation: Mapped[bool] = mapped_column(default=False)
    fvg_entry: Mapped[bool] = mapped_column(default=False)

    gross_rr: Mapped[float] = mapped_column(Float, default=0.0)
    net_rr: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_holding_hours: Mapped[float] = mapped_column(Float, default=6.0)

    analysis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    pending_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    estimated_net_pnl_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)

    telegram_approval_required: Mapped[bool] = mapped_column(default=True)
    telegram_approved: Mapped[bool | None] = mapped_column(nullable=True)
    telegram_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    telegram_approved_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    last_processed_candle_ms: Mapped[int] = mapped_column(Integer, default=0)
    entry_activation_candle_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    dedupe_key: Mapped[str] = mapped_column(String(128), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SignalEvent(Base):
    __tablename__ = "signal_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(Integer, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
