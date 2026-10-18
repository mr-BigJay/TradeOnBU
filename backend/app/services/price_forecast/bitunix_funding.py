from __future__ import annotations

from typing import Any


def parse_bitunix_funding_body(body: dict[str, Any], symbol: str) -> dict[str, Any]:
    """
    Bitunix funding_rate response — ``data`` may be a list of symbols or a single object.
    Missing fields stay null; no synthetic zeros.
    """
    if body.get("code") != 0:
        return {"status": "unavailable", "source": "bitunix", "reason": body.get("msg")}

    raw = body.get("data")
    rows: list[dict[str, Any]] = []
    if isinstance(raw, list):
        rows = [r for r in raw if isinstance(r, dict)]
    elif isinstance(raw, dict):
        rows = [raw]

    sym = symbol.upper()
    row = next((r for r in rows if str(r.get("symbol", "")).upper() == sym), None)
    if row is None:
        return {"status": "unavailable", "source": "bitunix", "reason": "symbol_not_in_response", "symbol": sym}

    def _opt_float(key: str) -> float | None:
        val = row.get(key)
        if val is None or val == "":
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    def _opt_int(key: str) -> int | None:
        val = row.get(key)
        if val is None or val == "":
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    out = {
        "status": "ok",
        "source": "bitunix",
        "symbol": sym,
        "funding_rate": _opt_float("fundingRate"),
        "mark_price": _opt_float("markPrice"),
        "index_price": _opt_float("indexPrice"),
        "last_price": _opt_float("lastPrice"),
        "next_funding_time_ms": _opt_int("nextFundingTime"),
        "funding_interval_hours": _opt_int("fundingInterval"),
    }
    required = ("funding_rate", "mark_price")
    if any(out.get(k) is None for k in required):
        out["status"] = "incomplete"
    return out
