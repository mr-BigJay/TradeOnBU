from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import RuntimeSettings


async def get_runtime_settings(session: AsyncSession) -> RuntimeSettings:
    result = await session.execute(select(RuntimeSettings).where(RuntimeSettings.id == 1))
    row = result.scalar_one_or_none()
    defaults = get_settings()
    if row is None:
        row = RuntimeSettings(
            id=1,
            max_pending_minutes=defaults.max_pending_minutes,
            max_missed_entry_pct=defaults.max_missed_entry_pct,
            holding_time_hours=defaults.holding_time_hours,
            min_net_rr=defaults.min_net_rr,
        )
        session.add(row)
        if session.in_nested_transaction():
            await session.flush()
        else:
            await session.commit()
        await session.refresh(row)
    return row
