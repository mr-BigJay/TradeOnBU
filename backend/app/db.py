from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_async_engine(settings.database_url, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


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
]


async def migrate_db() -> None:
    async with engine.begin() as conn:
        for stmt in SIGNAL_MIGRATIONS:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass


async def init_db() -> None:
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await migrate_db()
