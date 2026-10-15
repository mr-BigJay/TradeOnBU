from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()


def _sqlite_engine_kwargs(database_url: str) -> dict:
    if not database_url.startswith("sqlite"):
        return {}
    return {"connect_args": {"timeout": 30}}


engine = create_async_engine(
    settings.database_url,
    echo=False,
    **_sqlite_engine_kwargs(settings.database_url),
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def _configure_sqlite_connection(connection) -> None:
    if connection.dialect.name != "sqlite":
        return
    await connection.execute(text("PRAGMA journal_mode=WAL"))
    await connection.execute(text("PRAGMA busy_timeout=30000"))


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


SIGNAL_MIGRATIONS = [
    "ALTER TABLE signals ADD COLUMN telegram_approved_at DATETIME",
    "ALTER TABLE signals ADD COLUMN telegram_approved_by VARCHAR(32)",
    "ALTER TABLE signals ADD COLUMN telegram_message_id INTEGER",
    "ALTER TABLE signals ADD COLUMN last_processed_candle_ms INTEGER DEFAULT 0",
    "ALTER TABLE signals ADD COLUMN entry_activation_candle_ms INTEGER",
    "ALTER TABLE signals ADD COLUMN entry_monitor_from_ms INTEGER",
    "ALTER TABLE system_state ADD COLUMN worker_lock_until_ms INTEGER DEFAULT 0",
    "ALTER TABLE runtime_settings ADD COLUMN scan_interval_seconds INTEGER DEFAULT 300",
    "ALTER TABLE runtime_settings ADD COLUMN default_symbol VARCHAR(32) DEFAULT 'BTCUSDT'",
    "ALTER TABLE runtime_settings ADD COLUMN scanning_enabled BOOLEAN DEFAULT 1",
    "ALTER TABLE runtime_settings ADD COLUMN margin_usdt FLOAT DEFAULT 50.0",
    "ALTER TABLE runtime_settings ADD COLUMN leverage INTEGER DEFAULT 10",
    "ALTER TABLE runtime_settings ADD COLUMN maker_fee_rate FLOAT DEFAULT 0.0002",
    "ALTER TABLE runtime_settings ADD COLUMN taker_fee_rate FLOAT DEFAULT 0.0006",
    "ALTER TABLE runtime_settings ADD COLUMN spread_bps FLOAT DEFAULT 2.0",
    "ALTER TABLE runtime_settings ADD COLUMN slippage_bps FLOAT DEFAULT 5.0",
    "ALTER TABLE runtime_settings ADD COLUMN max_simultaneous_positions INTEGER DEFAULT 3",
    "ALTER TABLE runtime_settings ADD COLUMN paper_trading_paused BOOLEAN DEFAULT 0",
    "ALTER TABLE system_state ADD COLUMN ai_analysis_lock_owner VARCHAR(64)",
    "ALTER TABLE system_state ADD COLUMN ai_analysis_lock_until_ms INTEGER DEFAULT 0",
    "ALTER TABLE siwc_connect_sessions ADD COLUMN secret_hash VARCHAR(64)",
    "ALTER TABLE siwc_connect_sessions ADD COLUMN consumed_at DATETIME",
    "ALTER TABLE ai_analysis_settings ADD COLUMN unattended_consent BOOLEAN DEFAULT 0",
    "ALTER TABLE ai_analysis_settings ADD COLUMN unattended_consent_at DATETIME",
    "ALTER TABLE ai_analysis_settings ADD COLUMN interval_migrated_from_60 BOOLEAN DEFAULT 0",
    "ALTER TABLE ai_proposed_setups ADD COLUMN last_tracked_candle_ms INTEGER DEFAULT 0",
    "ALTER TABLE ai_analysis_settings ADD COLUMN auto_retry_pending BOOLEAN DEFAULT 0",
    "ALTER TABLE sim_positions ADD COLUMN quantity_btc FLOAT",
    "ALTER TABLE sim_positions ADD COLUMN execution_state VARCHAR(32) DEFAULT 'normal'",
    "ALTER TABLE sim_position_settings ADD COLUMN auto_open_paper_positions BOOLEAN DEFAULT 0",
    "ALTER TABLE ai_analysis_runs ADD COLUMN final_ai_decision VARCHAR(16)",
    "ALTER TABLE ai_analysis_runs ADD COLUMN setup_mode VARCHAR(16)",
    "ALTER TABLE ai_proposed_setups ADD COLUMN lifecycle_state VARCHAR(32) DEFAULT 'WAIT_LONG'",
    "ALTER TABLE ai_proposed_setups ADD COLUMN setup_mode VARCHAR(16) DEFAULT 'conditional'",
    "ALTER TABLE ai_proposed_setups ADD COLUMN final_ai_decision VARCHAR(16)",
    "ALTER TABLE ai_proposed_setups ADD COLUMN conditional_plan JSON",
    "ALTER TABLE ai_proposed_setups ADD COLUMN confirmation_state JSON",
    "ALTER TABLE ai_proposed_setups ADD COLUMN last_monitor_at DATETIME",
    "ALTER TABLE ai_proposed_setups ADD COLUMN last_rejection_reason VARCHAR(64)",
    "ALTER TABLE ai_proposed_setups ADD COLUMN last_validation_result JSON",
    "ALTER TABLE ai_proposed_setups ADD COLUMN monitor_cursor_ms INTEGER DEFAULT 0",
    "ALTER TABLE system_state ADD COLUMN opportunity_monitor_lock_owner VARCHAR(64)",
    "ALTER TABLE system_state ADD COLUMN opportunity_monitor_lock_until_ms INTEGER DEFAULT 0",
    "ALTER TABLE ai_proposed_setups ADD COLUMN monitoring_enabled BOOLEAN DEFAULT 1",
    "ALTER TABLE ai_proposed_setups ADD COLUMN monitor_from_ms INTEGER DEFAULT 0",
]

LEGACY_SETUP_BACKFILL = """
UPDATE ai_proposed_setups
SET
  monitoring_enabled = 0,
  setup_mode = COALESCE(setup_mode, 'legacy_immediate'),
  lifecycle_state = CASE
    WHEN status = 'invalidated' THEN 'INVALIDATED'
    WHEN status = 'expired' THEN 'EXPIRED'
    WHEN UPPER(direction) = 'SHORT' THEN 'READY_SHORT'
    ELSE 'READY_LONG'
  END
WHERE monitoring_enabled IS NULL
   OR (monitor_from_ms = 0 AND final_ai_decision IS NULL AND setup_mode = 'conditional')
   OR lifecycle_state = 'WAIT_LONG' AND final_ai_decision IS NULL
"""


async def migrate_db() -> None:
    async with engine.begin() as conn:
        for stmt in SIGNAL_MIGRATIONS:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass
        try:
            await conn.execute(text(LEGACY_SETUP_BACKFILL))
        except Exception:
            pass


async def init_db() -> None:
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        await _configure_sqlite_connection(conn)
        await conn.run_sync(Base.metadata.create_all)
    await migrate_db()
