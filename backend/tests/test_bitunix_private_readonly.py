"""Offline tests: read-only Bitunix futures private API and signing."""

import hashlib

import httpx
import pytest

from app.services.bitunix_private_readonly import (
    BitunixReadOnlyClient,
    BitunixReadOnlyError,
    sign_rest_request,
)


def test_sign_sorted_query_matches_official_double_sha256():
    nonce = "abc"
    ms = 1700000000123
    key, secret = "example_key", "example_secret"
    digest = hashlib.sha256(f"{nonce}{ms}{key}a1z9".encode()).hexdigest()
    expected = hashlib.sha256((digest + secret).encode()).hexdigest()
    assert sign_rest_request(
        api_key=key,
        secret_key=secret,
        nonce=nonce,
        timestamp_ms=ms,
        params={"z": "9", "a": "1"},
    ) == expected


@pytest.mark.asyncio
async def test_get_only_and_sanitized_probe_responses():
    routes = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "fapi.bitunix.com"
        assert request.headers["api-key"] == "test_key"
        assert request.headers["sign"]
        assert request.headers["timestamp"]
        assert request.headers["nonce"]
        routes.append(request.url.path)
        if request.url.path == "/api/v1/futures/account":
            assert request.url.params["marginCoin"] == "USDT"
            data = [{"marginCoin": "USDT", "available": "123.00", "positionMode": "HEDGE"}]
        elif request.url.path == "/api/v1/futures/position/get_pending_positions":
            data = [{"symbol": "BTCUSDT", "side": "LONG"}]
        elif request.url.path == "/api/v1/futures/trade/get_pending_orders":
            data = {"orderList": [{"symbol": "BTCUSDT", "status": "NEW"}], "total": 1}
        else:
            raise AssertionError("unexpected endpoint")
        return httpx.Response(200, json={"code": 0, "data": data})

    async with httpx.AsyncClient(
        base_url="https://fapi.bitunix.com", transport=httpx.MockTransport(handler)
    ) as http:
        cli = BitunixReadOnlyClient(
            api_key="test_key", secret_key="test_secret", http_client=http
        )
        assert (await cli.account_usdt())["available"] == "123.00"
        assert len(await cli.open_btc_positions()) == 1
        assert len(await cli.open_btc_orders()) == 1
        with pytest.raises(BitunixReadOnlyError):
            await cli._get("/api/v1/futures/trade/place_order", {})
    assert len(routes) == 3


@pytest.mark.asyncio
async def test_api_rejected_does_not_leak_secret_or_arbitrary_response_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 10001, "msg": "secret-secret"})

    async with httpx.AsyncClient(
        base_url="https://fapi.bitunix.com", transport=httpx.MockTransport(handler)
    ) as http:
        cli = BitunixReadOnlyClient(
            api_key="test_key", secret_key="test_secret", http_client=http
        )
        with pytest.raises(BitunixReadOnlyError) as e:
            await cli.account_usdt()
        assert "secret-secret" not in str(e.value)
        assert "test_secret" not in str(e.value)


@pytest.mark.asyncio
async def test_unexpected_response_is_fail_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"orderList": "not_a_list"}})

    async with httpx.AsyncClient(
        base_url="https://fapi.bitunix.com", transport=httpx.MockTransport(handler)
    ) as http:
        cli = BitunixReadOnlyClient(
            api_key="test_key", secret_key="test_secret", http_client=http
        )
        with pytest.raises(BitunixReadOnlyError, match="Unexpected"):
            await cli.open_btc_orders()
