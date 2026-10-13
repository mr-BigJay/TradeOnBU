import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.db import get_db
from app.main import app
from app.models import AiAnalysisSettings, ChatGptCredential, SiwcConnectSession
from app.services.siwc.connect_flow import hash_connect_secret, new_connect_secret
from app.services.siwc.responses_client import OpenAIResponsesError, stream_response


@pytest.mark.asyncio
async def test_public_connect_rejects_without_secret(client):
    resp = await client.post(
        "/api/public/siwc/connect/complete",
        json={
            "connect_token": "x" * 32,
            "oauth_state": "state123",
            "code": "code123456",
        },
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_public_connect_state_mismatch(session, client):
    secret, secret_hash = new_connect_secret()
    row = SiwcConnectSession(
        connect_token="tok" + "a" * 29,
        state="expected-state",
        nonce="nonce",
        code_verifier="verifier" * 8,
        redirect_uri="http://127.0.0.1:1455/auth/callback",
        secret_hash=secret_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    session.add(row)
    await session.commit()
    resp = await client.post(
        "/api/public/siwc/connect/complete",
        json={
            "connect_token": row.connect_token,
            "oauth_state": "wrong-state",
            "code": "code123456",
            "callback_client_id": "oaiapp_test",
        },
        headers={"X-Connect-Secret": secret},
    )
    assert resp.status_code == 400
    assert "state" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_public_connect_single_use(session, client):
    secret, secret_hash = new_connect_secret()
    row = SiwcConnectSession(
        connect_token="tok" + "b" * 29,
        state="state-st",
        nonce="nonce",
        code_verifier="verifier" * 8,
        redirect_uri="http://127.0.0.1:1455/auth/callback",
        secret_hash=secret_hash,
        consumed_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    session.add(row)
    await session.commit()
    resp = await client.post(
        "/api/public/siwc/connect/complete",
        json={
            "connect_token": row.connect_token,
            "oauth_state": "state-st",
            "code": "code123456",
            "callback_client_id": "oaiapp_test",
        },
        headers={"X-Connect-Secret": secret},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_public_connect_success_mocks_exchange(session, client):
    secret, secret_hash = new_connect_secret()
    row = SiwcConnectSession(
        connect_token="tok" + "c" * 29,
        state="good-state",
        nonce="nonce-val",
        code_verifier="verifier" * 8,
        redirect_uri="http://127.0.0.1:1455/auth/callback",
        secret_hash=secret_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    session.add(row)
    await session.commit()
    token_response = {
        "access_token": "at",
        "refresh_token": "rt",
        "scope": "openid chatgpt.tokens.use.direct offline_access",
        "expires_in": 3600,
        "id_token": "dummy",
    }
    claims = {"sub": "user-sub", "email": "u@example.com", "nonce": "nonce-val", "iss": "https://auth.openai.com"}
    with patch("app.services.siwc.connect_flow.exchange_code", AsyncMock(return_value=token_response)):
        with patch("app.services.siwc.connect_flow.validate_id_token", AsyncMock(return_value=claims)):
            with patch("app.services.siwc.credentials.encrypt_value", side_effect=lambda x: f"enc:{x}"):
                resp = await client.post(
                    "/api/public/siwc/connect/complete",
                    json={
                        "connect_token": row.connect_token,
                        "oauth_state": "good-state",
                        "code": "code123456",
                        "callback_client_id": "oaiapp_abc",
                    },
                    headers={"X-Connect-Secret": secret},
                )
    assert resp.status_code == 200
    assert resp.json()["email"] == "u@example.com"
    from sqlalchemy import select

    cred = (await session.execute(select(ChatGptCredential).where(ChatGptCredential.id == 1))).scalar_one()
    assert cred.client_id == "oaiapp_abc"
    assert "access_token" not in resp.text


@pytest.mark.asyncio
async def test_public_connect_plan_scope_denied(session, client):
    secret, secret_hash = new_connect_secret()
    row = SiwcConnectSession(
        connect_token="tok" + "d" * 29,
        state="state-st2",
        nonce="nonce2",
        code_verifier="verifier" * 8,
        redirect_uri="http://127.0.0.1:1455/auth/callback",
        secret_hash=secret_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    session.add(row)
    await session.commit()
    token_response = {
        "access_token": "at",
        "scope": "openid email",
        "expires_in": 3600,
        "id_token": "dummy",
    }
    with patch("app.services.siwc.connect_flow.exchange_code", AsyncMock(return_value=token_response)):
        with patch("app.services.siwc.connect_flow.validate_id_token", AsyncMock(return_value={"sub": "s", "nonce": "nonce2", "iss": "https://auth.openai.com"})):
            resp = await client.post(
                "/api/public/siwc/connect/complete",
                json={
                    "connect_token": row.connect_token,
                    "oauth_state": "state-st2",
                    "code": "code123456",
                    "callback_client_id": "oaiapp_x",
                },
                headers={"X-Connect-Secret": secret},
            )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_scheduling_requires_consent(client):
    resp = await client.put(
        "/api/gui/chatgpt/settings",
        json={"enabled": True, "unattended_consent": False, "interval_minutes": 60},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_stream_requires_completed():
    lines = [
        'data: {"type":"response.output_text.delta","delta":"Hi"}',
        "data: [DONE]",
    ]

    class FakeResp:
        status_code = 200

        async def aiter_lines(self):
            for line in lines:
                yield line

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    class StreamCM:
        async def __aenter__(self):
            return FakeResp()

        async def __aexit__(self, *_args):
            pass

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def stream(self, *_args, **_kwargs):
            return StreamCM()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    with patch("app.services.siwc.responses_client.httpx.AsyncClient", FakeClient):
        with pytest.raises(OpenAIResponsesError) as ei:
            await stream_response("tok", model="m", instructions="i", user_input="u")
        assert ei.value.code == "stream_interrupted"


@pytest.mark.asyncio
async def test_market_context_fail_closed(session):
    from app.services.ai_ict_analysis import MarketContextError, build_market_context

    with patch("app.services.ai_ict_analysis.MarketDataService.fetch_kline_bundle", AsyncMock(side_effect=RuntimeError("down"))):
        with pytest.raises(MarketContextError):
            await build_market_context("BTCUSDT")


@pytest.mark.asyncio
async def test_run_ai_skips_telegram_when_failed(session):
    from app.services.ai_ict_analysis import run_ai_analysis

    run = await run_ai_analysis(trigger="test")
    assert run.status == "failed"
    assert run.telegram_sent is False


@pytest.mark.asyncio
async def test_gui_connect_complete_success(session, client):
    secret, secret_hash = new_connect_secret()
    row = SiwcConnectSession(
        connect_token="tok" + "h" * 29,
        state="gui-state",
        nonce="nonce-gui",
        code_verifier="verifier" * 8,
        redirect_uri="http://127.0.0.1:1455/auth/callback",
        secret_hash=secret_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    session.add(row)
    await session.commit()
    token_response = {
        "access_token": "at",
        "refresh_token": "rt",
        "scope": "openid chatgpt.tokens.use.direct offline_access",
        "expires_in": 3600,
        "id_token": "dummy",
    }
    claims = {"sub": "gui-sub", "email": "gui@example.com", "nonce": "nonce-gui", "iss": "https://auth.openai.com"}
    with patch("app.services.siwc.connect_flow.exchange_code", AsyncMock(return_value=token_response)):
        with patch("app.services.siwc.connect_flow.validate_id_token", AsyncMock(return_value=claims)):
            with patch("app.services.siwc.credentials.encrypt_value", side_effect=lambda x: f"enc:{x}"):
                resp = await client.post(
                    "/api/gui/chatgpt/connect/complete",
                    json={
                        "connect_token": row.connect_token,
                        "connect_secret": secret,
                        "oauth_state": "gui-state",
                        "code": "code123456",
                        "callback_client_id": "oaiapp_gui",
                    },
                )
    assert resp.status_code == 200
    assert resp.json()["email"] == "gui@example.com"


@pytest.mark.asyncio
async def test_gui_connect_requires_session_when_enabled(monkeypatch, client):
    monkeypatch.setenv("SESSION_AUTH_ENABLED", "1")
    from app.config import get_settings

    get_settings.cache_clear()
    resp = await client.post("/api/gui/chatgpt/connect/begin")
    assert resp.status_code == 401
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_token_exchange_failure_marks_session_consumed(session, client):
    secret, secret_hash = new_connect_secret()
    row = SiwcConnectSession(
        connect_token="tok" + "f" * 29,
        state="state-stf",
        nonce="nf",
        code_verifier="verifier" * 8,
        redirect_uri="http://127.0.0.1:1455/auth/callback",
        secret_hash=secret_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    session.add(row)
    await session.commit()
    with patch("app.services.siwc.connect_flow.exchange_code", AsyncMock(side_effect=RuntimeError("invalid_grant"))):
        resp = await client.post(
            "/api/public/siwc/connect/complete",
            json={
                "connect_token": row.connect_token,
                "oauth_state": "state-stf",
                "code": "code123456",
                "callback_client_id": "oaiapp_x",
            },
            headers={"X-Connect-Secret": secret},
        )
    assert resp.status_code == 400
    await session.refresh(row)
    assert row.consumed_at is not None


@pytest.mark.asyncio
async def test_subject_mismatch_on_reconnect(session, client):
    secret, secret_hash = new_connect_secret()
    session.add(
        ChatGptCredential(
            id=1,
            subject="old-sub",
            client_id="oaiapp_old",
            access_token_enc="x",
            scopes_json=["chatgpt.tokens.use.direct"],
        )
    )
    row = SiwcConnectSession(
        connect_token="tok" + "g" * 29,
        state="state-stg",
        nonce="ng",
        code_verifier="verifier" * 8,
        redirect_uri="http://127.0.0.1:1455/auth/callback",
        secret_hash=secret_hash,
        issued_client_id="oaiapp_old",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    session.add(row)
    await session.commit()
    token_response = {
        "access_token": "at",
        "refresh_token": "rt",
        "scope": "chatgpt.tokens.use.direct openid",
        "expires_in": 3600,
        "id_token": "dummy",
    }
    with patch("app.services.siwc.connect_flow.exchange_code", AsyncMock(return_value=token_response)):
        with patch(
            "app.services.siwc.connect_flow.validate_id_token",
            AsyncMock(return_value={"sub": "new-sub", "email": "x@y.com", "nonce": "ng", "iss": "https://auth.openai.com"}),
        ):
            resp = await client.post(
                "/api/public/siwc/connect/complete",
                json={
                    "connect_token": row.connect_token,
                    "oauth_state": "state-stg",
                    "code": "code123456",
                    "callback_client_id": "oaiapp_old",
                },
                headers={"X-Connect-Secret": secret},
            )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_telegram_delivery_on_success(session):
    from app.models import ChatGptCredential
    from app.services.ai_ict_analysis import run_ai_analysis

    session.add(
        ChatGptCredential(
            id=1,
            client_id="oaiapp",
            access_token_enc="enc",
            refresh_token_enc="enc",
            scopes_json=["chatgpt.tokens.use.direct"],
            preferred_model="gpt-test",
            access_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    )
    await session.commit()
    ai_json = (
        '{"decision":"NO_TRADE","report_fa":"تست","entry_price":1,"stop_loss":1,"take_profit":1}'
    )
    cred_row = ChatGptCredential(
        id=1,
        client_id="oaiapp",
        preferred_model="gpt-test",
        scopes_json=["chatgpt.tokens.use.direct"],
    )
    with patch("app.services.ai_ict_analysis.get_access_token", AsyncMock(return_value=("tok", cred_row, None))):
        with patch("app.services.ai_ict_analysis.build_market_context", AsyncMock(return_value=({"live_price": 100}, 100.0))):
            with patch("app.services.ai_ict_analysis.stream_response", AsyncMock(return_value=f"```json\n{ai_json}\n```")):
                with patch("app.services.ai_ict_analysis.TelegramNotifier") as tn:
                    inst = tn.return_value
                    type(inst).enabled = property(lambda self: True)
                    inst.send_ai_report = AsyncMock(return_value=12345)
                    run = await run_ai_analysis(trigger="test")
    assert run.telegram_sent is True
    inst.send_ai_report.assert_awaited()
    args = inst.send_ai_report.await_args[0][0]
    assert "NO TRADE" in args or "تحلیل" in args


@pytest.mark.asyncio
async def test_should_run_scheduled_respects_interval(session):
    from app.services.ai_analysis_scheduler import should_run_scheduled

    cfg = AiAnalysisSettings(
        id=1,
        enabled=True,
        unattended_consent=True,
        interval_minutes=60,
        last_run_at=datetime.now(timezone.utc),
        next_run_at=datetime.now(timezone.utc) + timedelta(minutes=60),
    )
    session.add(cfg)
    cred = ChatGptCredential(
        id=1,
        client_id="oaiapp_x",
        access_token_enc="enc",
        scopes_json=["chatgpt.tokens.use.direct"],
    )
    session.add(cred)
    await session.commit()
    assert await should_run_scheduled(session) is False
