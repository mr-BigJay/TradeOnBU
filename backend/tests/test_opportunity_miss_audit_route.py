from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.db import init_db

pytest_plugins = ("tests.test_phase3_auth",)


@pytest.mark.asyncio
async def test_miss_audit_route_without_replay(session_app):
    await init_db()
    login = await session_app.post("/api/auth/login", json={"username": "admin", "password": "SecurePass123"})
    assert login.status_code == 200
    cookies = login.cookies
    resp = await session_app.get(
        "/api/gui/opportunities/miss-audit?symbol=BTCUSDT&include_replay=false",
        cookies=cookies,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "summary" in body
    assert "rows" in body
    assert body["summary"]["total_setups"] >= 0
    if body["rows"]:
        assert all(r.get("replay_status") == "not_run" for r in body["rows"])
        assert body["summary"].get("replay_not_run_count", 0) >= 1


@pytest.mark.asyncio
async def test_miss_audit_route_with_replay_mock(session_app):
    await init_db()
    login = await session_app.post("/api/auth/login", json={"username": "admin", "password": "SecurePass123"})
    cookies = login.cookies
    fake_replay = {
        "kline_5m_limit": 120,
        "data_complete_5m": True,
        "gaps_remaining_5m": [],
        "replay_diffs": [],
        "report": {"summary": {"total_setups": 0}, "by_outcome": {}, "rows": []},
    }
    with patch(
        "app.services.opportunity_miss_runner.replay_open_setups",
        new=AsyncMock(return_value=fake_replay),
    ):
        resp = await session_app.get(
            "/api/gui/opportunities/miss-audit?symbol=BTCUSDT&include_replay=true",
            cookies=cookies,
        )
    assert resp.status_code == 200
    assert "replay_meta" in resp.json()
