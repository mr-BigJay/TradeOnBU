from __future__ import annotations

import hashlib


def build_dedupe_key(
    exchange: str,
    symbol: str,
    event_ms: int,
    native_side: str,
    price: float,
    qty: float,
    *,
    extra: str = "",
) -> str:
    raw = f"{exchange}|{symbol}|{event_ms}|{native_side}|{price:.8f}|{qty:.8f}|{extra}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]
