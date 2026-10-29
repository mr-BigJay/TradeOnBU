"""Explicit, allowlisted Bitunix BTC futures snapshot for the admin dashboard.

Private credentials never leave the backend; data is deliberately projected to
specific fields instead of forwarding raw exchange payloads.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.bitunix_private_readonly import BitunixReadOnlyClient


def _amount(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = Decimal(str(value))
        if not num.is_finite():
            return None
        return str(num)
    except (ValueError, InvalidOperation):
        return None


def _percent(numerator: Any, denominator: Any) -> str | None:
    try:
        a, b = Decimal(str(numerator)), Decimal(str(denominator))
        if not (a.is_finite() and b.is_finite()) or b <= 0:
            return None
        return str(round(a * 100 / b, 2))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _timestamp(v: Any) -> int | None:
    try:
        val = int(v)
        return val if 0 < val < 100000000000000 else None
    except (ValueError, TypeError):
        return None


def project_snapshot(
    *,
    account: dict[str, Any],
    leverage: dict[str, Any],
    positions: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    tpsl_orders: list[dict[str, Any]],
    history: dict[str, Any],
) -> dict[str, Any]:
    if account.get("marginCoin") != "USDT" or leverage.get("symbol") != "BTCUSDT":
        raise ValueError("Unexpected exchange account or symbol")
    cross = _amount(account.get("crossUnrealizedPNL"))
    isolation = _amount(account.get("isolationUnrealizedPNL"))
    pnl = _amount(Decimal(cross or "0") + Decimal(isolation or "0"))
    result_positions = [{
        "id": str(p.get("positionId") or ""),
        "symbol": "BTCUSDT",
        "side": p.get("side") if p.get("side") in ("LONG", "SHORT") else "UNKNOWN",
        "leverage": _amount(p.get("leverage")),
        "margin_mode": p.get("marginMode"),
        "qty": _amount(p.get("qty")),
        "entry_price": _amount(p.get("avgOpenPrice")),
        "margin": _amount(p.get("margin")),
        "entry_value": _amount(p.get("entryValue")),
        "unrealized_pnl": _amount(p.get("unrealizedPNL")),
        "realized_pnl": _amount(p.get("realizedPNL")),
        "roi_pct": _percent(p.get("unrealizedPNL"), p.get("margin")),
        "liq_price": _amount(p.get("liqPrice")),
        "margin_rate": _amount(p.get("marginRate")),
        "fee": _amount(p.get("fee")),
        "funding": _amount(p.get("funding")),
        "created_at": _timestamp(p.get("ctime")),
    } for p in positions]
    result_orders = [{
        "id": str(o.get("orderId") or ""),
        "side": o.get("side"),
        "type": o.get("orderType") or o.get("type"),
        "status": o.get("status"),
        "qty": _amount(o.get("qty")),
        "filled_qty": _amount(o.get("tradeQty")),
        "price": _amount(o.get("price")),
        "leverage": _amount(o.get("leverage")),
        "margin_mode": o.get("marginMode"),
        "reduce_only": o.get("reduceOnly") is True,
        "tp_price": _amount(o.get("tpPrice")),
        "sl_price": _amount(o.get("slPrice")),
        "created_at": _timestamp(o.get("ctime")),
    } for o in orders]
    result_tpsl = [{
        "id": str(x.get("id") or ""),
        "position_id": str(x.get("positionId") or ""),
        "tp_price": _amount(x.get("tpPrice")),
        "sl_price": _amount(x.get("slPrice")),
        "tp_qty": _amount(x.get("tpQty")),
        "sl_qty": _amount(x.get("slQty")),
        "tp_type": x.get("tpOrderType"),
        "sl_type": x.get("slOrderType"),
    } for x in tpsl_orders]
    result_history = [{
        "id": str(h.get("positionId") or ""),
        "side": h.get("side") if h.get("side") in ("LONG", "SHORT") else "UNKNOWN",
        "leverage": _amount(h.get("leverage")),
        "margin_mode": h.get("marginMode"),
        "qty": _amount(h.get("maxQty")),
        "entry_price": _amount(h.get("entryPrice")),
        "close_price": _amount(h.get("closePrice")),
        "realized_pnl": _amount(h.get("realizedPNL")),
        "fee": _amount(h.get("fee")),
        "funding": _amount(h.get("funding")),
        "closed_at": _timestamp(h.get("mtime")),
    } for h in history["positions"]]
    result_history.sort(key=lambda h: h["closed_at"] or 0, reverse=True)
    return {
        "configured": True,
        "read_only": True,
        "live_trading": False,
        "symbol": "BTCUSDT",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "account": {
            "currency": "USDT",
            "available": _amount(account.get("available")),
            "frozen": _amount(account.get("frozen")),
            "position_margin": _amount(account.get("margin")),
            "unrealized_pnl": pnl,
            "cross_unrealized_pnl": cross,
            "isolated_unrealized_pnl": isolation,
            "transferable": _amount(account.get("transfer")),
            "bonus": _amount(account.get("bonus")),
            "position_mode": account.get("positionMode"),
        },
        "settings": {
            "leverage": _amount(leverage.get("leverage")),
            "margin_mode": leverage.get("marginMode"),
        },
        "positions": result_positions,
        "orders": result_orders,
        "tpsl_orders": result_tpsl,
        "history": {
            "positions": result_history,
            "displayed": len(result_history),
            "total": history.get("total") if isinstance(history.get("total"), int) else None,
        },
    }


async def get_bitunix_dashboard_snapshot() -> dict[str, Any]:
    api_key = os.getenv("BITUNIX_MONITOR_API_KEY", "").strip()
    secret_key = os.getenv("BITUNIX_MONITOR_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        return {"configured": False, "read_only": True, "live_trading": False}
    cli = BitunixReadOnlyClient(api_key=api_key, secret_key=secret_key)
    # Six read-only GET calls, in parallel; far below Bitunix's 10 req/sec/uid limit.
    account, leverage, positions, orders, tpsl, history = await asyncio.gather(
        cli.account_usdt(),
        cli.btc_leverage_settings(),
        cli.open_btc_positions(),
        cli.open_btc_orders(),
        cli.btc_pending_tpsl(),
        cli.btc_position_history(limit=20),
    )
    return project_snapshot(
        account=account, leverage=leverage, positions=positions,
        orders=orders, tpsl_orders=tpsl, history=history,
    )
