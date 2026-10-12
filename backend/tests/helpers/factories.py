from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.models import Signal, SignalStatus


def make_signal(**overrides) -> Signal:
    now = datetime.now(timezone.utc)
    data = {
        "public_id": str(uuid4()),
        "symbol": "BTCUSDT",
        "status": SignalStatus.WAITING_FOR_ENTRY.value,
        "direction": "LONG",
        "entry_type": "LIMIT",
        "entry_price": 100.0,
        "entry_zone_low": 99.5,
        "entry_zone_high": 100.5,
        "stop_loss": 98.0,
        "take_profit": 106.0,
        "invalidation_level": 98.0,
        "market_bias": "BULLISH",
        "liquidity_sweep": True,
        "mss_confirmation": True,
        "fvg_entry": True,
        "gross_rr": 3.0,
        "net_rr": 2.5,
        "estimated_holding_hours": 6.0,
        "analysis": {},
        "pending_expires_at": now + timedelta(minutes=90),
        "telegram_approval_required": True,
        "telegram_approved": None,
        "dedupe_key": str(uuid4()),
        "last_processed_candle_ms": 0,
    }
    data.update(overrides)
    return Signal(**data)
