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
    worker_lock_until_ms: Mapped[int] = mapped_column(Integer, default=0)
    ai_analysis_lock_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_analysis_lock_until_ms: Mapped[int] = mapped_column(Integer, default=0)
    opportunity_monitor_lock_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opportunity_monitor_lock_until_ms: Mapped[int] = mapped_column(Integer, default=0)


class RuntimeSettings(Base):
    __tablename__ = "runtime_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    max_pending_minutes: Mapped[int] = mapped_column(Integer, default=90)
    max_missed_entry_pct: Mapped[float] = mapped_column(Float, default=0.30)
    holding_time_hours: Mapped[float] = mapped_column(Float, default=6.0)
    min_net_rr: Mapped[float] = mapped_column(Float, default=2.0)
    scan_interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    default_symbol: Mapped[str] = mapped_column(String(32), default="BTCUSDT")
    scanning_enabled: Mapped[bool] = mapped_column(default=True)
    margin_usdt: Mapped[float] = mapped_column(Float, default=50.0)
    leverage: Mapped[int] = mapped_column(Integer, default=10)
    maker_fee_rate: Mapped[float] = mapped_column(Float, default=0.0002)
    taker_fee_rate: Mapped[float] = mapped_column(Float, default=0.0006)
    spread_bps: Mapped[float] = mapped_column(Float, default=2.0)
    slippage_bps: Mapped[float] = mapped_column(Float, default=5.0)
    max_simultaneous_positions: Mapped[int] = mapped_column(Integer, default=3)
    paper_trading_paused: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class InstallationMeta(Base):
    __tablename__ = "installation_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    installed: Mapped[bool] = mapped_column(default=False)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    installed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(default=True)
    onboarding: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SettingEntry(Base):
    __tablename__ = "setting_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    key: Mapped[str] = mapped_column(String(64), index=True)
    value_plain: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SiwcConnectSession(Base):
    __tablename__ = "siwc_connect_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connect_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    state: Mapped[str] = mapped_column(String(64))
    nonce: Mapped[str] = mapped_column(String(64))
    code_verifier: Mapped[str] = mapped_column(String(128))
    redirect_uri: Mapped[str] = mapped_column(String(255))
    issued_client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    secret_hash: Mapped[str] = mapped_column(String(64))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatGptCredential(Base):
    __tablename__ = "chatgpt_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(128), nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ext_agent_host_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scopes_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    id_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    preferred_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AiAnalysisSettings(Base):
    __tablename__ = "ai_analysis_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(default=False)
    unattended_consent: Mapped[bool] = mapped_column(default=False)
    unattended_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    interval_migrated_from_60: Mapped[bool] = mapped_column(default=False)
    auto_retry_pending: Mapped[bool] = mapped_column(default=False)


class AiProposedSetup(Base):
    __tablename__ = "ai_proposed_setups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    setup_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), index=True, default="open")
    thesis_fa: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_zone_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_zone_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    invalidation: Mapped[str | None] = mapped_column(Text, nullable=True)
    invalidation_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    entry_touched: Mapped[bool] = mapped_column(default=False)
    sl_touched: Mapped[bool] = mapped_column(default=False)
    tp_touched: Mapped[bool] = mapped_column(default=False)
    sequencing_ambiguous: Mapped[bool] = mapped_column(default=False)
    paper_fill_recorded: Mapped[bool] = mapped_column(default=False)
    objective_notes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    structured_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_tracked_candle_ms: Mapped[int] = mapped_column(Integer, default=0)
    lifecycle_state: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    setup_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    monitoring_enabled: Mapped[bool] = mapped_column(default=True)
    monitor_from_ms: Mapped[int] = mapped_column(Integer, default=0)
    final_ai_decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    conditional_plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confirmation_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_monitor_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_rejection_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_validation_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    monitor_cursor_ms: Mapped[int] = mapped_column(Integer, default=0)


class IctSetupAuditEvent(Base):
    __tablename__ = "ict_setup_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    setup_id: Mapped[str] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AiAnalysisRun(Base):
    __tablename__ = "ai_analysis_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    trigger: Mapped[str] = mapped_column(String(32), default="scheduled")
    model_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    final_ai_decision: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    setup_mode: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    report_fa: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    telegram_sent: Mapped[bool] = mapped_column(default=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_ai: Mapped[bool] = mapped_column(default=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class SettingsAuditLog(Base):
    __tablename__ = "settings_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


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
    entry_monitor_from_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    dedupe_key: Mapped[str] = mapped_column(String(128), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    signal_public_id: Mapped[str] = mapped_column(String(36), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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


class SimPositionSettings(Base):
    __tablename__ = "sim_position_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    default_margin_usdt: Mapped[float] = mapped_column(Float, default=50.0)
    default_leverage: Mapped[int] = mapped_column(Integer, default=10)
    position_mode: Mapped[str] = mapped_column(String(16), default="isolated")
    default_order_type: Mapped[str] = mapped_column(String(16), default="market")
    default_sl_behavior: Mapped[str] = mapped_column(String(32), default="market")
    default_tp_behavior: Mapped[str] = mapped_column(String(32), default="market")
    fee_mode: Mapped[str] = mapped_column(String(32), default="bitunix_vip1")
    maker_fee_rate: Mapped[float] = mapped_column(Float, default=0.0002)
    taker_fee_rate: Mapped[float] = mapped_column(Float, default=0.0005)
    fee_source_url: Mapped[str] = mapped_column(String(255), default="https://www.bitunix.com/service/handling-fee")
    fee_verified_on: Mapped[str | None] = mapped_column(String(16), nullable=True)
    slippage_bps: Mapped[float] = mapped_column(Float, default=5.0)
    max_margin_per_position: Mapped[float] = mapped_column(Float, default=500.0)
    max_risk_pct_per_position: Mapped[float] = mapped_column(Float, default=5.0)
    initial_balance_set: Mapped[bool] = mapped_column(default=False)
    auto_open_paper_positions: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SimWalletLedger(Base):
    __tablename__ = "sim_wallet_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_type: Mapped[str] = mapped_column(String(32), index=True)
    amount_usdt: Mapped[float] = mapped_column(Float)
    balance_after: Mapped[float] = mapped_column(Float)
    reference_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class SimPosition(Base):
    __tablename__ = "sim_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True, default="BTCUSDT")
    direction: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending_entry")
    execution_state: Mapped[str] = mapped_column(String(32), default="normal", index=True)
    order_type: Mapped[str] = mapped_column(String(16), default="market")
    margin_usdt: Mapped[float] = mapped_column(Float)
    leverage: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    limit_entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity_btc: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_setup_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    entry_fee_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    exit_fee_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    funding_fee_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    gross_pnl_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_pnl_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_is_maker: Mapped[bool] = mapped_column(default=False)
    exit_is_maker: Mapped[bool] = mapped_column(default=False)
    last_processed_candle_ms: Mapped[int] = mapped_column(Integer, default=0)
    monitor_from_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transition_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SimPositionEvent(Base):
    __tablename__ = "sim_position_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(Integer, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class SimEquitySnapshot(Base):
    __tablename__ = "sim_equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    equity_usdt: Mapped[float] = mapped_column(Float)
    wallet_balance: Mapped[float] = mapped_column(Float)
    margin_in_use: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
