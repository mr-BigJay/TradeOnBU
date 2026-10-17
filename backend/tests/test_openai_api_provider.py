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



@pytest.mark.asyncio
async def test_dashboard_api_key_storage_and_provider_switch(session, client, monkeypatch):
    from app.services.settings_store import get_secret, get_plain
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()

    save = await client.put("/api/gui/chatgpt/api/key", json={"api_key": "sk-test-dashboard-secret-123456"})
    assert save.status_code == 200
    assert "sk-test-dashboard-secret-123456" not in save.text
    assert await get_secret(session, "ai", "openai_api_key") == "sk-test-dashboard-secret-123456"

    enabled = await client.put("/api/gui/chatgpt/provider", json={"provider": "openai_api"})
    assert enabled.status_code == 200
    assert await get_plain(session, "ai", "provider") == "openai_api"

    status = await client.get("/api/gui/chatgpt/status")
    assert status.status_code == 200
    assert status.json()["ai_provider"] == "openai_api"
    assert status.json()["api_key_configured"] is True
    assert "sk-test-dashboard-secret-123456" not in status.text

    with patch("app.routes.chatgpt_ai.api_model_accessible", AsyncMock(return_value=True)) as checker:
        checked = await client.post("/api/gui/chatgpt/api/test")
        assert checked.status_code == 200
        checker.assert_awaited_once_with("sk-test-dashboard-secret-123456", "gpt-6-luna")

    remove = await client.delete("/api/gui/chatgpt/api/key")
    assert remove.status_code == 200
    assert await get_secret(session, "ai", "openai_api_key") == ""
    status_after = await client.get("/api/gui/chatgpt/status")
    assert status_after.json()["api_key_configured"] is False


@pytest.mark.asyncio
async def test_dashboard_api_key_route_rejects_bad_format(session, client):
    response = await client.put("/api/gui/chatgpt/api/key", json={"api_key": "this-is-not-an-openai-key"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_dashboard_api_key_never_downgrades_to_siwc(session, client, monkeypatch):
    from app.services.ai_analysis_scheduler import get_or_create_settings, scheduling_allowed
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    await client.put("/api/gui/chatgpt/api/key", json={"api_key": "sk-test-long-enough-key-001"})
    await client.put("/api/gui/chatgpt/provider", json={"provider": "openai_api"})
    cfg = await get_or_create_settings(session)
    cfg.enabled = True
    cfg.unattended_consent = True
    await session.commit()
    assert await scheduling_allowed(session, cfg) == (True, None)
    await client.delete("/api/gui/chatgpt/api/key")
    assert await scheduling_allowed(session, cfg) == (False, "openai_api_key_missing")


@pytest.mark.asyncio
async def test_luna_max_persists_and_status_and_connection_test_reflect_it(session, client, monkeypatch):
    from app.services.settings_store import get_plain

    monkeypatch.setenv("AI_PROVIDER", "openai_api")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    get_settings.cache_clear()

    response = await client.put("/api/gui/chatgpt/api/reasoning", json={"effort": "max"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "model": "gpt-6-luna", "reasoning_effort": "max"}
    assert await get_plain(session, "ai", "api_reasoning_effort") == "max"

    status = await client.get("/api/gui/chatgpt/status")
    assert status.status_code == 200
    assert status.json()["ai_analysis_model"] == "gpt-6-luna"
    assert status.json()["ai_reasoning_effort"] == "max"

    with patch("app.routes.chatgpt_ai.api_model_accessible", AsyncMock(return_value=True)) as probe:
        checked = await client.post("/api/gui/chatgpt/api/test")
    assert checked.status_code == 200
    assert checked.json()["reasoning_effort"] == "max"
    probe.assert_awaited_once_with("sk-test-only", "gpt-6-luna")

    restored = await client.put("/api/gui/chatgpt/api/reasoning", json={"effort": "high"})
    assert restored.status_code == 200
    assert (await client.get("/api/gui/chatgpt/status")).json()["ai_reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_luna_max_sent_to_responses_without_using_siwc(session, client, monkeypatch):
    from app.services.ai_ict_analysis import _execute_inference

    monkeypatch.setenv("AI_PROVIDER", "openai_api")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    get_settings.cache_clear()
    saved = await client.put("/api/gui/chatgpt/api/reasoning", json={"effort": "max"})
    assert saved.status_code == 200
    captured = {}

    async def inspect_request(access_token, **kwargs):
        captured["token"] = access_token
        captured.update(kwargs)
        raise OpenAIResponsesError("test_stop", "stop after verifying request")

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
                    draft = await _execute_inference(run_id=123, trigger="scheduled", model_override=None)

    siwc.assert_not_called()
    assert captured["token"] == "sk-test-only"
    assert captured["model"] == "gpt-6-luna"
    assert captured["reasoning_effort"] == "max"
    assert json.loads(captured["user_input"])["requested_inference"]["reasoning_effort"] == "max"
    assert draft.error_code == "test_stop"


@pytest.mark.asyncio
async def test_api_reasoning_validation_rejects_unapproved_levels(session, client, monkeypatch):
    from app.services.settings_store import get_plain

    monkeypatch.setenv("AI_PROVIDER", "openai_api")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    get_settings.cache_clear()

    for invalid_effort in ["low", "xhigh", "none", "sol", "MAX"]:
        response = await client.put("/api/gui/chatgpt/api/reasoning", json={"effort": invalid_effort})
        assert response.status_code == 422
    assert await get_plain(session, "ai", "api_reasoning_effort", "high") == "high"

    assert (await client.put("/api/gui/chatgpt/provider", json={"provider": "siwc"})).status_code == 200
    rejected = await client.put("/api/gui/chatgpt/api/reasoning", json={"effort": "max"})
    assert rejected.status_code == 409
    assert await get_plain(session, "ai", "api_reasoning_effort", "high") == "high"


@pytest.mark.asyncio
async def test_api_max_selection_independent_of_siwc(session, client, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai_api")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    get_settings.cache_clear()

    assert (await client.put("/api/gui/chatgpt/api/reasoning", json={"effort": "max"})).status_code == 200
    assert (await client.put("/api/gui/chatgpt/provider", json={"provider": "siwc"})).status_code == 200
    assert (await client.put("/api/gui/chatgpt/provider", json={"provider": "openai_api"})).status_code == 200

    status = await client.get("/api/gui/chatgpt/status")
    assert status.status_code == 200
    assert status.json()["ai_reasoning_effort"] == "max"
    assert status.json()["ai_analysis_model"] == "gpt-6-luna"


def test_unknown_luna_api_effort_defaults_to_high():
    from app.services.ai_api_reasoning import effective_luna_api_effort
    assert effective_luna_api_effort(None) == "high"
    assert effective_luna_api_effort("xhigh") == "high"
    assert effective_luna_api_effort("high") == "high"
    assert effective_luna_api_effort("max") == "max"
