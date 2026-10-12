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
