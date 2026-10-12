from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "TradeOnBU ICT"
    # Relative path for local runs from backend/; Docker overrides with /app/data absolute URL.
    database_url: str = "sqlite+aiosqlite:///./data/tradeonbu.db"

    bitunix_futures_base: str = "https://fapi.bitunix.com"
    default_symbol: str = "BTCUSDT"

    margin_usdt: float = 50.0
    leverage: int = 10
    # Bitunix VIP1 taker/maker (editable via env)
    taker_fee_rate: float = 0.0006
    maker_fee_rate: float = 0.0002
    slippage_bps: float = 5.0
    spread_bps: float = 2.0

    max_pending_minutes: int = 90
    max_missed_entry_pct: float = 0.30
    holding_time_hours: float = 6.0
    min_net_rr: float = 2.0

    scan_interval_seconds: int = 300

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_require_approval: bool = True
    telegram_allowed_user_ids: str = ""
    telegram_webhook_secret: str = ""
    telegram_use_webhook: bool = False
    dashboard_public_url: str = "http://127.0.0.1:8000"

    market_data_stale_seconds: int = 120

    dashboard_api_key: str = ""
    admin_api_key: str = ""
    cors_allowed_origins: str = "http://127.0.0.1:8000,http://localhost:8000,http://localhost:5173"
    rate_limit_per_minute: int = 120

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def _strip_allowlist(cls, v: str) -> str:
        return v or ""

    def allowed_telegram_users(self) -> set[int]:
        ids: set[int] = set()
        for part in self.telegram_allowed_user_ids.split(","):
            part = part.strip()
            if part.isdigit():
                ids.add(int(part))
        return ids

    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    def effective_telegram_webhook(self) -> bool:
        if not self.telegram_use_webhook:
            return False
        secret = self.telegram_webhook_secret or ""
        return len(secret) >= 32


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_effective_settings() -> Settings:
    """Environment bootstrap merged with DB-backed overlay (Telegram, risk, etc.)."""
    from app.services.settings_runtime import overlay_get

    base = get_settings().model_copy()
    token = overlay_get("telegram_bot_token")
    if token:
        base.telegram_bot_token = token
    chat = overlay_get("telegram_chat_id")
    if chat:
        base.telegram_chat_id = chat
    allow = overlay_get("telegram_allowed_user_ids")
    if allow is not None and allow != "":
        base.telegram_allowed_user_ids = allow
    req = overlay_get("telegram_require_approval")
    if req:
        base.telegram_require_approval = req.lower() == "true"
    wh = overlay_get("telegram_use_webhook")
    if wh:
        base.telegram_use_webhook = wh.lower() == "true"
    sec = overlay_get("telegram_webhook_secret")
    if sec:
        base.telegram_webhook_secret = sec
    sym = overlay_get("default_symbol")
    if sym:
        base.default_symbol = sym
    scan = overlay_get("scan_interval_seconds")
    if scan.isdigit():
        base.scan_interval_seconds = int(scan)
    for fld in ("margin_usdt", "leverage", "maker_fee_rate", "taker_fee_rate", "spread_bps", "slippage_bps"):
        val = overlay_get(fld)
        if val:
            setattr(base, fld, type(getattr(base, fld))(val))
    return base
