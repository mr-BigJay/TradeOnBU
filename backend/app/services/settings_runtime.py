from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services.runtime_settings import get_runtime_settings
from app.services.settings_store import apply_telegram_to_env_cache

_settings_overlay: dict[str, str] = {}


async def refresh_settings_cache(session: AsyncSession) -> None:
    global _settings_overlay
    _settings_overlay = await apply_telegram_to_env_cache(session)
    row = await get_runtime_settings(session)
    _settings_overlay.update(
        {
            "default_symbol": row.default_symbol,
            "scan_interval_seconds": str(row.scan_interval_seconds),
            "margin_usdt": str(row.margin_usdt),
            "leverage": str(row.leverage),
            "maker_fee_rate": str(row.maker_fee_rate),
            "taker_fee_rate": str(row.taker_fee_rate),
            "spread_bps": str(row.spread_bps),
            "slippage_bps": str(row.slippage_bps),
        }
    )
    get_settings.cache_clear()


def overlay_get(name: str, default: str = "") -> str:
    return _settings_overlay.get(name, default)
