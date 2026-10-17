import json
from unittest.mock import AsyncMock, patch

import pytest

from app.config import get_settings
from app.services.ai_analysis_scheduler import get_or_create_settings, scheduling_allowed
from app.services.siwc.responses_client import OpenAIResponsesError


@pytest.mark.asyncio
async def test_api_provider_scheduling_without_siwc(session, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai_api")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    get_settings.cache_clear()
    cfg = await get_or_create_settings(session)
    cfg.enabled = True
    cfg.unattended_consent = True
    await session.commit()
    assert await scheduling_allowed(session, cfg) == (True, None)


@pytest.mark.asyncio
async def test_api_provider_requires_server_key(session, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai_api")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    cfg = await get_or_create_settings(session)
    cfg.enabled = True
    cfg.unattended_consent = True
    await session.commit()
    assert await scheduling_allowed(session, cfg) == (False, "openai_api_key_missing")


@pytest.mark.asyncio
async def test_api_provider_uses_luna_high_and_never_calls_siwc(session, monkeypatch):
    from app.services.ai_ict_analysis import _execute_inference

    monkeypatch.setenv("AI_PROVIDER", "openai_api")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    get_settings.cache_clear()

    captured = {}

    async def inspect_request(access_token, **kwargs):
        captured["token"] = access_token
        captured.update(kwargs)
        raise OpenAIResponsesError("test_stop", "stop after verifying API routing")

    with patch("app.services.ai_ict_analysis.get_access_token", AsyncMock()) as siwc:
        with patch(
            "app.services.ai_ict_analysis.build_raw_market_payload",
            AsyncMock(return_value=({"symbol": "BTCUSDT"}, 100.0, {"15m": []})),
        ):
            with patch(
                "app.services.ai_ict_analysis.build_ai_memory_context",
                AsyncMock(return_value={}),
            ):
                with patch(
                    "app.services.ai_ict_analysis.stream_analysis_response",
                    side_effect=inspect_request,
                ):
                    draft = await _execute_inference(run_id=123, trigger="test", model_override=None)

    siwc.assert_not_called()
    assert captured["token"] == "sk-test-only"
    assert captured["model"] == "gpt-6-luna"
    assert captured["reasoning_effort"] == "high"
    assert json.loads(captured["user_input"])["requested_inference"]["reasoning_effort"] == "high"
    assert draft.error_code == "test_stop"


@pytest.mark.asyncio
async def test_api_status_never_returns_secret(session, client, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai_api")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-do-not-return")
    get_settings.cache_clear()
    response = await client.get("/api/gui/chatgpt/status")
    assert response.status_code == 200
    data = response.json()
    assert data["ai_provider"] == "openai_api"
    assert data["api_key_configured"] is True
    assert data["ai_analysis_model"] == "gpt-6-luna"
    assert data["ai_reasoning_effort"] == "high"
    assert "sk-test-secret" not in response.text


@pytest.mark.asyncio
async def test_api_key_test_does_not_spend_tokens(session, client, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai_api")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    get_settings.cache_clear()
    with patch(
        "app.routes.chatgpt_ai.api_model_accessible",
        AsyncMock(return_value=True),
    ) as check:
        response = await client.post("/api/gui/chatgpt/api/test")
    assert response.status_code == 200
    check.assert_awaited_once_with("sk-test-only", "gpt-6-luna")
    assert response.json()["reasoning_effort"] == "high"
    assert "sk-test-only" not in response.text


@pytest.mark.asyncio
async def test_api_settings_enables_scheduler_without_siwc(session, client, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai_api")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    get_settings.cache_clear()
    response = await client.put(
        "/api/gui/chatgpt/settings",
        json={"enabled": True, "unattended_consent": True, "interval_minutes": 60},
    )
    assert response.status_code == 200
    cfg = await get_or_create_settings(session)
    await session.refresh(cfg)
    assert cfg.enabled is True


@pytest.mark.asyncio
async def test_api_mode_rejects_model_switch_to_costly_sol(session, client, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai_api")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    get_settings.cache_clear()
    response = await client.put("/api/gui/chatgpt/model", json={"model": "gpt-6.1-sol"})
    assert response.status_code == 409
