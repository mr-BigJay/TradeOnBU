"""Small, allowlisted Bitunix futures private REST reader.

This module intentionally exposes GET-only account/position/order queries.
It cannot submit, cancel, close, transfer, or modify anything.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any, Mapping

import httpx

BITUNIX_FUTURES_BASE_URL = "https://fapi.bitunix.com"
_ALLOWED_GET_PATHS = frozenset({
    "/api/v1/futures/account",
    "/api/v1/futures/position/get_pending_positions",
    "/api/v1/futures/trade/get_pending_orders",
})


class BitunixReadOnlyError(Exception):
    """A sanitized connection/auth/API failure; never contains credentials."""


def sign_rest_request(
    *,
    api_key: str,
    secret_key: str,
    nonce: str,
    timestamp_ms: int,
    params: Mapping[str, str],
) -> str:
    """Bitunix REST: sha256(sha256(nonce+timestamp+key+sortedQuery+body)+secret)."""
    canonical_query = "".join(f"{name}{params[name]}" for name in sorted(params))
    first = hashlib.sha256(
        f"{nonce}{timestamp_ms}{api_key}{canonical_query}".encode("utf-8")
    ).hexdigest()
    return hashlib.sha256((first + secret_key).encode("utf-8")).hexdigest()


class BitunixReadOnlyClient:
    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip() or not secret_key.strip():
            raise ValueError("Bitunix credentials are required")
        self._api_key = api_key.strip()
        self._secret_key = secret_key.strip()
        self._http = http_client

    async def _get(self, path: str, params: Mapping[str, str]) -> Any:
        if path not in _ALLOWED_GET_PATHS:
            raise BitunixReadOnlyError("Endpoint not allowed by read-only client")
        timestamp_ms = time.time_ns() // 1_000_000
        nonce = secrets.token_hex(8)
        params = dict(params)
        headers = {
            "api-key": self._api_key,
            "nonce": nonce,
            "timestamp": str(timestamp_ms),
            "sign": sign_rest_request(
                api_key=self._api_key,
                secret_key=self._secret_key,
                nonce=nonce,
                timestamp_ms=timestamp_ms,
                params=params,
            ),
            "Content-Type": "application/json",
        }
        owned = self._http is None
        client = self._http or httpx.AsyncClient(
            base_url=BITUNIX_FUTURES_BASE_URL,
            timeout=10.0,
            follow_redirects=False,
        )
        try:
            try:
                response = await client.get(path, params=params, headers=headers)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise BitunixReadOnlyError(f"Bitunix HTTP {exc.response.status_code}") from None
            except httpx.RequestError:
                raise BitunixReadOnlyError("Cannot reach Bitunix futures API") from None
            try:
                payload = response.json()
            except ValueError:
                raise BitunixReadOnlyError("Invalid JSON returned by Bitunix") from None
            if not isinstance(payload, dict) or str(payload.get("code")) != "0":
                code = payload.get("code") if isinstance(payload, dict) else "unknown"
                raise BitunixReadOnlyError(f"Bitunix request rejected (code={str(code)[:24]})")
            return payload.get("data")
        finally:
            if owned:
                await client.aclose()

    async def account_usdt(self) -> dict[str, Any]:
        data = await self._get("/api/v1/futures/account", {"marginCoin": "USDT"})
        if isinstance(data, list):
            match = next(
                (x for x in data if isinstance(x, dict) and x.get("marginCoin") == "USDT"),
                None,
            )
        elif isinstance(data, dict):
            match = data if data.get("marginCoin") == "USDT" else None
        else:
            match = None
        if match is None:
            raise BitunixReadOnlyError("USDT futures account missing in API response")
        return match

    async def open_btc_positions(self) -> list[dict[str, Any]]:
        data = await self._get(
            "/api/v1/futures/position/get_pending_positions", {"symbol": "BTCUSDT"}
        )
        if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
            raise BitunixReadOnlyError("Unexpected open positions response")
        return [x for x in data if x.get("symbol") == "BTCUSDT"]

    async def open_btc_orders(self) -> list[dict[str, Any]]:
        data = await self._get(
            "/api/v1/futures/trade/get_pending_orders", {"symbol": "BTCUSDT"}
        )
        if not isinstance(data, dict) or not isinstance(data.get("orderList"), list):
            raise BitunixReadOnlyError("Unexpected pending orders response")
        return [
            x for x in data["orderList"]
            if isinstance(x, dict) and x.get("symbol") == "BTCUSDT"
        ]
