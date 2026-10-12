from __future__ import annotations

from app.config import Settings, get_effective_settings
from app.services.settings_runtime import overlay_get


def telegram_gui_enabled() -> bool:
    return overlay_get("telegram_enabled", "false").lower() == "true"


def telegram_integration_active(settings: Settings | None = None) -> bool:
    """True when GUI enabled and token + chat id are configured."""
    if not telegram_gui_enabled():
        return False
    s = settings or get_effective_settings()
    return bool(s.telegram_bot_token and s.telegram_chat_id)
