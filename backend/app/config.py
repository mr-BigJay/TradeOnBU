from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "TradeOnBU ICT"
    database_url: str = "sqlite+aiosqlite:///./data/tradeonbu.db"

    # Bitunix futures (public market data only — no live orders)
    bitunix_futures_base: str = "https://fapi.bitunix.com"
    default_symbol: str = "BTCUSDT"

    # Paper trading assumptions
    margin_usdt: float = 50.0
    leverage: int = 10
    taker_fee_rate: float = 0.0006
    maker_fee_rate: float = 0.0002
    slippage_bps: float = 5.0
    spread_bps: float = 2.0

    # Lifecycle defaults (overridable via dashboard / DB)
    max_pending_minutes: int = 90
    max_missed_entry_pct: float = 0.30
    holding_time_hours: float = 6.0
    min_net_rr: float = 2.0

    scan_interval_seconds: int = 300

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_require_approval: bool = True

    market_data_stale_seconds: int = 120


@lru_cache
def get_settings() -> Settings:
    return Settings()
