from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RuntimeSettings, SettingEntry, SettingsAuditLog
from app.services.crypto_secrets import decrypt_value, encrypt_value


TELEGRAM_KEYS = {
    "enabled",
    "bot_token",
    "chat_id",
    "allowed_user_ids",
    "require_approval",
    "use_webhook",
    "webhook_secret",
}


async def _get_entry(session: AsyncSession, category: str, key: str) -> SettingEntry | None:
    return (
        await session.execute(
            select(SettingEntry).where(SettingEntry.category == category, SettingEntry.key == key)
        )
    ).scalar_one_or_none()


async def set_plain(session: AsyncSession, category: str, key: str, value: str) -> None:
    row = await _get_entry(session, category, key)
    if row is None:
        row = SettingEntry(category=category, key=key)
        session.add(row)
    row.value_plain = value
    row.value_secret = None
    row.updated_at = datetime.now(timezone.utc)


async def set_secret(session: AsyncSession, category: str, key: str, value: str) -> None:
    row = await _get_entry(session, category, key)
    if row is None:
        row = SettingEntry(category=category, key=key)
        session.add(row)
    row.value_secret = encrypt_value(value) if value else None
    row.value_plain = None
    row.updated_at = datetime.now(timezone.utc)


async def get_plain(session: AsyncSession, category: str, key: str, default: str = "") -> str:
    row = await _get_entry(session, category, key)
    if not row or row.value_plain is None:
        return default
    return row.value_plain


async def get_secret(session: AsyncSession, category: str, key: str, default: str = "") -> str:
    row = await _get_entry(session, category, key)
    if not row or not row.value_secret:
        return default
    return decrypt_value(row.value_secret)


async def audit(session: AsyncSession, user_id: int | None, category: str, action: str, detail: dict) -> None:
    session.add(SettingsAuditLog(user_id=user_id, category=category, action=action, detail=detail))


async def telegram_public_view(session: AsyncSession) -> dict[str, Any]:
    token = await get_secret(session, "telegram", "bot_token")
    masked = ""
    if token:
        masked = token[:4] + "…" + token[-4:] if len(token) > 8 else "****"
    enabled = (await get_plain(session, "telegram", "enabled", "false")).lower() == "true"
    configured = bool(token and await get_plain(session, "telegram", "chat_id"))
    return {
        "enabled": enabled,
        "bot_token_masked": masked,
        "chat_id": await get_plain(session, "telegram", "chat_id"),
        "allowed_user_ids": await get_plain(session, "telegram", "allowed_user_ids"),
        "require_approval": (await get_plain(session, "telegram", "require_approval", "true")).lower() == "true",
        "use_webhook": (await get_plain(session, "telegram", "use_webhook", "false")).lower() == "true",
        "status": "configured" if configured and enabled else ("not_configured" if not configured else "disabled"),
    }


async def apply_telegram_to_env_cache(session: AsyncSession) -> dict[str, str]:
    """Values for in-process Settings refresh (not written to .env)."""
    return {
        "telegram_enabled": await get_plain(session, "telegram", "enabled", "false"),
        "telegram_bot_token": await get_secret(session, "telegram", "bot_token"),
        "telegram_chat_id": await get_plain(session, "telegram", "chat_id"),
        "telegram_allowed_user_ids": await get_plain(session, "telegram", "allowed_user_ids"),
        "telegram_require_approval": await get_plain(session, "telegram", "require_approval", "true"),
        "telegram_use_webhook": await get_plain(session, "telegram", "use_webhook", "false"),
        "telegram_webhook_secret": await get_secret(session, "telegram", "webhook_secret"),
    }


async def sync_runtime_row(session: AsyncSession, row: RuntimeSettings) -> RuntimeSettings:
    """Ensure runtime row exists with defaults (never overwrite on migrate)."""
    existing = (await session.execute(select(RuntimeSettings).where(RuntimeSettings.id == 1))).scalar_one_or_none()
    if existing:
        return existing
    session.add(row)
    await session.flush()
    return row
