from __future__ import annotations

import os
import secrets

from app.config import Settings


class ConfigSecurityError(RuntimeError):
    pass


def validate_settings(settings: Settings) -> None:
    if os.getenv("SKIP_CONFIG_GUARD") == "1":
        return

    if os.getenv("SESSION_AUTH_ENABLED") == "1":
        if os.getenv("SESSION_COOKIE_SECURE") != "1" and os.getenv("ALLOW_INSECURE_SESSION") != "1":
            raise ConfigSecurityError("SESSION_COOKIE_SECURE=1 is required when SESSION_AUTH_ENABLED=1")
        if not (os.getenv("INTERNAL_SERVICE_KEY") or "").strip() or len(os.getenv("INTERNAL_SERVICE_KEY", "")) < 32:
            raise ConfigSecurityError("INTERNAL_SERVICE_KEY must be set (min 32 chars) for production session auth")
        return

    if not settings.dashboard_api_key or len(settings.dashboard_api_key) < 16:
        raise ConfigSecurityError("DASHBOARD_API_KEY must be set (min 16 chars)")
    if not settings.admin_api_key or len(settings.admin_api_key) < 16:
        raise ConfigSecurityError("ADMIN_API_KEY must be set (min 16 chars)")
    if settings.dashboard_api_key == settings.admin_api_key:
        raise ConfigSecurityError("DASHBOARD_API_KEY and ADMIN_API_KEY must differ")

    if settings.telegram_bot_token:
        allow = settings.allowed_telegram_users()
        if not allow:
            raise ConfigSecurityError("TELEGRAM_ALLOWED_USER_IDS must be set when Telegram is enabled")

    if settings.telegram_use_webhook:
        secret = settings.telegram_webhook_secret
        if not secret or len(secret) < 32:
            raise ConfigSecurityError("TELEGRAM_WEBHOOK_SECRET must be >= 32 chars when webhook mode is enabled")
    elif settings.telegram_webhook_secret and len(settings.telegram_webhook_secret) < 32:
        raise ConfigSecurityError("TELEGRAM_WEBHOOK_SECRET is too weak")


def generate_webhook_secret() -> str:
    return secrets.token_urlsafe(48)
