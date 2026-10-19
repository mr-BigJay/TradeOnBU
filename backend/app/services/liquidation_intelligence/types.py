from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NormalizedLiquidation:
    source_exchange: str
    symbol: str
    event_timestamp_ms: int
    received_timestamp_ms: int
    liquidated_position_side: str  # long | short
    native_order_side: str
    price: float
    quantity_btc: float
    estimated_notional_usd: float
    price_type: str
    source_quality: str
    dedupe_key: str
    notes: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "dedupe_key": self.dedupe_key,
            "source_exchange": self.source_exchange,
            "symbol": self.symbol,
            "event_timestamp_ms": self.event_timestamp_ms,
            "received_timestamp_ms": self.received_timestamp_ms,
            "liquidated_position_side": self.liquidated_position_side,
            "native_order_side": self.native_order_side,
            "price": self.price,
            "quantity_btc": self.quantity_btc,
            "estimated_notional_usd": self.estimated_notional_usd,
            "price_type": self.price_type,
            "source_quality": self.source_quality,
        }
