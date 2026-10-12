import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import AiAnalysisRun, SystemState
from app.services.ai_analysis_executor import execute_ai_analysis_with_lock
from app.services.ai_analysis_scheduler import acquire_analysis_lock, is_analysis_lock_held, release_analysis_lock
from app.services.ai_telegram_format import format_ai_telegram_message


@pytest.mark.asyncio
async def test_lock_acquire_and_release_commits(session):
    owner = "test-owner-a"
    assert await acquire_analysis_lock(owner, ttl_seconds=60) is True
    assert await is_analysis_lock_held() is True
    assert await release_analysis_lock(owner) is True
    assert await is_analysis_lock_held() is False


@pytest.mark.asyncio
async def test_lock_release_after_exception():
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("inference failed")

    with patch("app.services.ai_analysis_executor.run_ai_analysis", _boom):
        with pytest.raises(RuntimeError):
            await execute_ai_analysis_with_lock(trigger="manual")
    assert await is_analysis_lock_held() is False


@pytest.mark.asyncio
async def test_concurrent_second_acquire_busy(session):
    owner1 = "test-owner-c1"
    owner2 = "test-owner-c2"
    assert await acquire_analysis_lock(owner1, ttl_seconds=120)
    assert await acquire_analysis_lock(owner2, ttl_seconds=120) is False
    await release_analysis_lock(owner1)
    assert await acquire_analysis_lock(owner2, ttl_seconds=120) is True
    await release_analysis_lock(owner2)


@pytest.mark.asyncio
async def test_execute_returns_busy_when_locked():
    assert await acquire_analysis_lock("blocker", ttl_seconds=120)
    try:
        with patch("app.services.ai_analysis_executor.run_ai_analysis", AsyncMock()) as mock_run:
            outcome = await execute_ai_analysis_with_lock(trigger="manual")
        assert outcome.status == "busy"
        assert outcome.run is None
        mock_run.assert_not_called()
    finally:
        await release_analysis_lock("blocker")


@pytest.mark.asyncio
async def test_analyze_now_http_409_when_busy(client):
    assert await acquire_analysis_lock("http-blocker", ttl_seconds=120)
    try:
        resp = await client.post("/api/gui/chatgpt/analyze-now")
        assert resp.status_code == 409
        assert "running" in resp.json()["detail"].lower()
    finally:
        await release_analysis_lock("http-blocker")


@pytest.mark.asyncio
async def test_connect_import_disabled(client):
    resp = await client.post(
        "/api/gui/chatgpt/connect/import",
        json={
            "subject": "x",
            "client_id": "y",
            "access_token": "z",
            "id_token": "t",
            "scopes": ["chatgpt.tokens.use.direct"],
        },
    )
    assert resp.status_code == 404


def test_telegram_invalid_trade_label():
    msg = format_ai_telegram_message(
        decision="LONG",
        report_fa="گزارش",
        validation={"ok": False, "reasons": ["net_rr_below_threshold"]},
    )
    assert "INVALID" in msg
    assert "NOT APPROVED" in msg
    assert "NO TRADE" in msg
    assert "NOT actionable" in msg
    assert "net_rr_below_threshold" in msg


@pytest.mark.asyncio
async def test_telegram_invalid_on_rejected_long(session):
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
    cred_row = ChatGptCredential(id=1, client_id="oaiapp", preferred_model="gpt-test", scopes_json=["chatgpt.tokens.use.direct"])
    ai_json = (
        '{"decision":"LONG","report_fa":"پیشنهاد","entry_price":100,"stop_loss":99,"take_profit":101}'
    )
    sent: list[str] = []

    async def capture(msg: str):
        sent.append(msg)
        return 1

    with patch("app.services.ai_ict_analysis.get_access_token", AsyncMock(return_value=("tok", cred_row, None))):
        with patch("app.services.ai_ict_analysis.build_market_context", AsyncMock(return_value=({"live_price": 100}, 100.0))):
            with patch("app.services.ai_ict_analysis.stream_response", AsyncMock(return_value=f"```json\n{ai_json}\n```")):
                with patch("app.services.ai_ict_analysis.TelegramNotifier") as tn:
                    inst = tn.return_value
                    inst.enabled = True
                    inst.send_message = AsyncMock(side_effect=capture)
                    run = await run_ai_analysis(session, trigger="test")
    assert run.status == "completed_with_warnings"
    assert sent
    assert "INVALID" in sent[0]


@pytest.mark.asyncio
async def test_lock_persisted_in_separate_committed_transaction(session):
    """Acquire/release each commit independently; other sessions see lock row without sharing txn."""
    from app.db import SessionLocal

    owner = "txn-owner"
    assert await acquire_analysis_lock(owner, ttl_seconds=60)
    async with SessionLocal() as other:
        st = (await other.execute(select(SystemState).where(SystemState.id == 1))).scalar_one()
        assert st.ai_analysis_lock_owner == owner
        other.add(AiAnalysisRun(status="pending", trigger="parallel-write", is_ai=False))
        await other.commit()
    assert await release_analysis_lock(owner)
    async with SessionLocal() as other:
        st = (await other.execute(select(SystemState).where(SystemState.id == 1))).scalar_one()
        assert st.ai_analysis_lock_owner is None
