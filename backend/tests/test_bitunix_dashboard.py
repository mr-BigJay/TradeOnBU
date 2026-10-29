"""No-network safety and shape tests for the live Bitunix account dashboard."""
import httpx
import pytest

from app.services.bitunix_dashboard import get_bitunix_dashboard_snapshot, project_snapshot
from app.services.bitunix_private_readonly import BitunixReadOnlyClient, BitunixReadOnlyError


@pytest.mark.asyncio
async def test_no_credentials_returns_unconfigured_not_account(monkeypatch):
    monkeypatch.delenv("BITUNIX_MONITOR_API_KEY", raising=False)
    monkeypatch.delenv("BITUNIX_MONITOR_SECRET_KEY", raising=False)
    data = await get_bitunix_dashboard_snapshot()
    assert data == {"configured": False, "read_only": True, "live_trading": False}
    assert "account" not in data


def test_snapshot_projects_only_known_fields_and_exposes_real_margin_leverage():
    data = project_snapshot(
        account={
            "marginCoin": "USDT", "available": "120", "frozen": "5",
            "margin": "30", "positionMode": "HEDGE", "crossUnrealizedPNL": "1.5",
            "isolationUnrealizedPNL": "-0.5", "transfer": "100", "bonus": "0",
            "api_key": "MUST_NOT_LEAK",
        },
        leverage={"symbol": "BTCUSDT", "marginMode": "ISOLATION", "leverage": 10},
        positions=[{
            "positionId": "pos1", "side": "SHORT", "leverage": 5,
            "marginMode": "ISOLATION", "qty": "0.003", "margin": "50",
            "unrealizedPNL": "10", "avgOpenPrice": "82703.1",
            "liqPrice": "100000", "secret_key": "MUST_NOT_LEAK",
        }],
        orders=[{
            "orderId": "ord1", "symbol": "BTCUSDT", "status": "NEW_",
            "side": "SELL", "orderType": "LIMIT", "qty": "0.003",
            "tradeQty": "0", "leverage": 5, "marginMode": "ISOLATION",
            "price": "82703.1", "tpPrice": None, "slPrice": None,
        }],
        tpsl_orders=[{
            "id": "sl1", "positionId": "pos1", "symbol": "BTCUSDT",
            "slPrice": "83000", "slQty": "0.003",
        }],
        history={"positions": [{
            "positionId": "done", "side": "LONG", "leverage": 10,
            "maxQty": "0.1", "entryPrice": "82000", "closePrice": "83000",
            "realizedPNL": "10", "fee": "1", "funding": "-0.2",
            "mtime": 1720000000000,
        }], "total": 1},
    )
    assert data["account"]["unrealized_pnl"] == "1.0"
    assert data["positions"][0]["roi_pct"] == "20.00"
    assert data["positions"][0]["leverage"] == "5"
    assert data["settings"]["leverage"] == "10"
    assert data["orders"][0]["price"] == "82703.1"
    assert data["tpsl_orders"][0]["sl_price"] == "83000"
    assert data["history"]["positions"][0]["realized_pnl"] == "10"
    assert data["live_trading"] is False
    assert "MUST_NOT_LEAK" not in str(data)
    assert data["positions"][0]["liq_price"] == "100000"


@pytest.mark.asyncio
async def test_all_six_requests_are_get_only_and_allowed():
    seen = []
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "fapi.bitunix.com"
        seen.append(request.url.path)
        path = request.url.path
        if path == "/api/v1/futures/account":
            response = [{"marginCoin": "USDT", "available": "5"}]
        elif path == "/api/v1/futures/account/get_leverage_margin_mode":
            assert request.url.params.get("symbol") == "BTCUSDT"
            response = {"symbol": "BTCUSDT", "leverage": 5, "marginMode": "ISOLATION"}
        elif path == "/api/v1/futures/position/get_history_positions":
            assert request.url.params.get("limit") == "20"
            response = {"positionList": [], "total": 0}
        elif path == "/api/v1/futures/position/get_pending_positions":
            response = []
        elif path == "/api/v1/futures/trade/get_pending_orders":
            response = {"orderList": []}
        elif path == "/api/v1/futures/tpsl/get_pending_orders":
            response = []
        else:
            raise AssertionError(f"Unknown endpoint {path}")
        return httpx.Response(200, json={"code": 0, "data": response})

    async with httpx.AsyncClient(
        base_url="https://fapi.bitunix.com",
        transport=httpx.MockTransport(handler),
    ) as http:
        c = BitunixReadOnlyClient(api_key="test-key", secret_key="secret-key", http_client=http)
        await c.account_usdt()
        await c.btc_leverage_settings()
        await c.open_btc_positions()
        await c.open_btc_orders()
        await c.btc_pending_tpsl()
        await c.btc_position_history(limit=20)
        with pytest.raises(BitunixReadOnlyError):
            await c._get("/api/v1/futures/trade/place_order", {})
    assert len(seen) == 6
