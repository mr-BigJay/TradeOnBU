"""Runtime paper entry mode (STRICT vs BALANCED + shadow)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entry_execution import ENTRY_MODE_BALANCED, ENTRY_MODE_STRICT, normalize_entry_mode
from app.services.runtime_settings import get_runtime_settings


async def get_paper_entry_execution_config(
    session: AsyncSession,
) -> tuple[str, bool]:
    row = await get_runtime_settings(session)
    mode = normalize_entry_mode(getattr(row, "paper_entry_mode", None))
    shadow = bool(getattr(row, "balanced_entry_shadow_mode", False))
    return mode, shadow


def live_confirmation_engine_mode(paper_entry_mode: str, balanced_shadow: bool) -> str:
    """
    Engine that mutates setup.confirmation_state and lifecycle.
    Shadow keeps live on STRICT while BALANCED runs in balanced_shadow_sim only.
    """
    mode = normalize_entry_mode(paper_entry_mode)
    if mode == ENTRY_MODE_BALANCED and balanced_shadow:
        return ENTRY_MODE_STRICT
    return mode


def live_order_engine_mode(paper_entry_mode: str, balanced_shadow: bool) -> str:
    """Paper orders follow the same rule as live confirmations."""
    return live_confirmation_engine_mode(paper_entry_mode, balanced_shadow)


def shadow_simulation_enabled(paper_entry_mode: str, balanced_shadow: bool) -> bool:
    return normalize_entry_mode(paper_entry_mode) == ENTRY_MODE_BALANCED and balanced_shadow
