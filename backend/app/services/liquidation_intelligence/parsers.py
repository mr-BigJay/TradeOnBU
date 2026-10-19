from __future__ import annotations

import math
import time
from typing import Any

from app.services.liquidation_intelligence.dedupe import build_dedupe_key
from app.services.liquidation_intelligence.types import NormalizedLiquidation


class LiquidationParseError(Exception):
    pass


def _finite_pos(value: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def bybit_side_to_liquidated_position(native: str) -> str:
    """Bybit allLiquidation: Buy = long liquidated, Sell = short liquidated."""
    s = (native or "").strip().lower()
    if s == "buy":
        return "long"
    if s == "sell":
        return "short"
    raise LiquidationParseError(f"bybit_unknown_side:{native}")


def binance_side_to_liquidated_position(native: str) -> str:
    """Binance forceOrder order side: SELL closes long, BUY closes short."""
    s = (native or "").strip().upper()
    if s == "SELL":
        return "long"
    if s == "BUY":
        return "short"
    raise LiquidationParseError(f"binance_unknown_side:{native}")


def parse_bybit_all_liquidation_message(msg: dict[str, Any], *, symbol: str = "BTCUSDT") -> list[NormalizedLiquidation]:
    topic = str(msg.get("topic") or "")
    if "allLiquidation" not in topic and "allliquidation" not in topic.lower():
        return []
    if symbol.upper() not in topic.upper():
        return []
    received_ms = int(time.time() * 1000)
    out: list[NormalizedLiquidation] = []
    rows = msg.get("data") or []
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("s") or symbol).upper()
        if sym != symbol.upper():
            continue
        try:
            event_ms = int(row.get("T") or 0)
            native = str(row.get("S") or "")
            qty = float(row.get("v") or 0)
            price = float(row.get("p") or 0)
        except (TypeError, ValueError) as exc:
            raise LiquidationParseError("bybit_invalid_fields") from exc
        if not _finite_pos(qty) or not _finite_pos(price) or event_ms <= 0:
            continue
        liq_side = bybit_side_to_liquidated_position(native)
        dedupe = build_dedupe_key("bybit", sym, event_ms, native, price, qty)
        out.append(
            NormalizedLiquidation(
                source_exchange="bybit",
                symbol=sym,
                event_timestamp_ms=event_ms,
                received_timestamp_ms=received_ms,
                liquidated_position_side=liq_side,
                native_order_side=native,
                price=price,
                quantity_btc=qty,
                estimated_notional_usd=price * qty,
                price_type="bankruptcy_price",
                source_quality="observed_full_tape",
                dedupe_key=dedupe,
                notes="Bybit bankruptcy price is not guaranteed exact fill price for entire position",
            )
        )
    return out


def parse_binance_force_order_message(msg: dict[str, Any], *, symbol: str = "BTCUSDT") -> NormalizedLiquidation | None:
    if str(msg.get("e") or "").lower() != "forceorder":
        return None
    order = msg.get("o")
    if not isinstance(order, dict):
        raise LiquidationParseError("binance_missing_order")
    sym = str(order.get("s") or "").upper()
    if sym != symbol.upper():
        return None
    received_ms = int(time.time() * 1000)
    event_ms = int(msg.get("E") or order.get("T") or 0)
    status = str(order.get("X") or "").upper()
    native = str(order.get("S") or "")
    try:
        executed = float(order.get("z") or order.get("l") or 0)
        avg_price = float(order.get("ap") or order.get("p") or 0)
    except (TypeError, ValueError) as exc:
        raise LiquidationParseError("binance_invalid_qty_price") from exc
    if status and status not in ("FILLED", "PARTIALLY_FILLED"):
        return None
    if not _finite_pos(executed) or not _finite_pos(avg_price) or event_ms <= 0:
        return None
    liq_side = binance_side_to_liquidated_position(native)
    quality = "sampled_1000ms_window" if status == "FILLED" else "partial_execution"
    dedupe = build_dedupe_key(
        "binance",
        sym,
        event_ms,
        native,
        avg_price,
        executed,
        extra=str(order.get("i") or order.get("c") or ""),
    )
    return NormalizedLiquidation(
        source_exchange="binance",
        symbol=sym,
        event_timestamp_ms=event_ms,
        received_timestamp_ms=received_ms,
        liquidated_position_side=liq_side,
        native_order_side=native,
        price=avg_price,
        quantity_btc=executed,
        estimated_notional_usd=avg_price * executed,
        price_type="avg_fill_price",
        source_quality=quality,
        dedupe_key=dedupe,
        notes="Binance forceOrder stream may omit liquidations within the same 1000ms window",
    )
