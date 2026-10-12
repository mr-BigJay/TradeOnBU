import pytest

from app.services.siwc.oauth import has_plan_scope, new_pkce, build_authorize_url


def test_pkce_authorize_url_contains_required_params():
    pkce = new_pkce()
    url = build_authorize_url(
        ext_agent_host_id="urn:uuid:test",
        redirect_uri="http://127.0.0.1:1455/auth/callback",
        pkce=pkce,
    )
    assert "dynamic_agent_client" in url
    assert "chatgpt.tokens.use.direct" in url
    assert "TradeOnBU" in url
    assert pkce.state in url


def test_plan_scope_detection():
    assert has_plan_scope(["openid", "chatgpt.tokens.use.direct"])
    assert not has_plan_scope(["openid"])


@pytest.mark.asyncio
async def test_ai_run_fails_without_credentials(session):
    from app.services.ai_ict_analysis import run_ai_analysis

    run = await run_ai_analysis(trigger="test")
    assert run.status == "failed"
    assert run.is_ai is True
    assert run.error_code == "not_connected"
