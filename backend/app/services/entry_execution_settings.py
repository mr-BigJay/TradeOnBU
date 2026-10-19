"""Runtime paper entry mode (STRICT vs BALANCED + shadow)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entry_execution import normalize_entry_mode
from app.services.runtime_settings import get_runtime_settings


async def get_paper_entry_execution_config(
    session: AsyncSession,
) -> tuple[str, bool]:
    row = await get_runtime_settings(session)
    mode = normalize_entry_mode(getattr(row, "paper_entry_mode", None))
    shadow = bool(getattr(row, "balanced_entry_shadow_mode", False))
    return mode, shadow
