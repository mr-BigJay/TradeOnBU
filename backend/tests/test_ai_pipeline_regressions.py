import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import AiAnalysisRun, AiAnalysisSettings, ChatGptCredential
from app.services.ai_analysis_scheduler import FAILURE_BACKOFF_MINUTES, should_run_scheduled
from app.services.ai_ict_analysis import run_ai_analysis
from app.services.ai_telegram_format import format_ai_telegram_message, split_ai_telegram_report_messages
from app.services.ai_analysis_scheduler import record_run_outcome
from app.services.telegram_notifier import TelegramNotifier


@pytest.mark.asyncio
async def test_no_open_write_during_delayed_openai(session):
    gate = asyncio.Event()
    proceed = asyncio.Event()

    async def slow_stream(*_args, **_kwargs):
        gate.set()
        await proceed.wait()
        return '```json\n{"decision":"NO_TRADE","report_fa":"ok"}\n```'

    cred = ChatGptCredential(
        id=1,
        client_id="oai",
        access_token_enc="enc",
        refresh_token_enc="enc",
        scopes_json=["chatgpt.tokens.use.direct"],
        preferred_model="m",
        access_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    session.add(cred)
    await session.commit()

    async def parallel_writer():
        await gate.wait()
        async with SessionLocal() as s:
            s.add(AiAnalysisRun(status="pending", trigger="probe", is_ai=False))
            await s.commit()

    async def _skip_chart(**kwargs):
        return None, kwargs["structured"]

    with patch("app.services.ai_ict_analysis.get_access_token", AsyncMock(return_value=("tok", cred, None))):
        from app.services.siwc.responses_client import AnalysisResponse

        async def slow_analysis(*_args, **_kwargs):
            gate.set()
            await proceed.wait()
            return AnalysisResponse(
                text='```json\n{"decision":"NO_TRADE","report_fa":"ok"}\n```',
                requested_model="gpt-6.1-sol",
                requested_reasoning_effort="max",
                response_model="gpt-6.1-sol",
            )

        with patch(
            "app.services.ai_ict_analysis.build_raw_market_payload",
            AsyncMock(return_value=({"x": 1, "timeframes": {}}, 100.0, {})),
        ):
            async def memory_side(*_args, **_kwargs):
                gate.set()
                return {"open_setups": []}

            with patch("app.services.ai_ict_analysis.build_ai_memory_context", memory_side):
                with patch("app.services.ai_ict_analysis._generate_chart_for_run", _skip_chart):
                    with patch("app.services.ai_ict_analysis._deliver_ai_telegram", AsyncMock()):
                        with patch("app.services.ai_ict_analysis.stream_analysis_response", slow_analysis):
                            task = asyncio.create_task(run_ai_analysis(trigger="manual"))
                            await asyncio.wait_for(parallel_writer(), timeout=3.0)
                            proceed.set()
                            run = await task
    assert run.status.startswith("completed")
    probes = (await session.execute(select(AiAnalysisRun).where(AiAnalysisRun.trigger == "probe"))).scalars().all()
    assert probes


def test_record_run_outcome_sets_future_next_attempt():
    cfg = AiAnalysisSettings(interval_minutes=60)
    record_run_outcome(cfg, "completed", trigger="scheduled")
    assert cfg.next_run_at > datetime.now(timezone.utc) + timedelta(minutes=59)
    record_run_outcome(cfg, "failed", "openai_error", trigger="scheduled")
    assert cfg.next_run_at > datetime.now(timezone.utc) + timedelta(minutes=FAILURE_BACKOFF_MINUTES - 1)


@pytest.mark.asyncio
async def test_stale_past_next_run_fixed_after_failure_persisted(session):
    cfg = AiAnalysisSettings(
        id=1,
        enabled=True,
        unattended_consent=True,
        interval_minutes=60,
        next_run_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    session.add(cfg)
    session.add(
        ChatGptCredential(
            id=1,
            client_id="oai",
            access_token_enc="enc",
            scopes_json=["chatgpt.tokens.use.direct"],
        )
    )
    await session.commit()
    assert await should_run_scheduled(session) is True
    record_run_outcome(cfg, "failed", "quota", trigger="scheduled")
    await session.commit()
    await session.refresh(cfg)
    nra = cfg.next_run_at
    if nra.tzinfo is None:
        nra = nra.replace(tzinfo=timezone.utc)
    assert nra > datetime.now(timezone.utc)
    assert await should_run_scheduled(session) is False


@pytest.mark.asyncio
async def test_failed_run_backoff_blocks_minute_scheduler(session):
    now = datetime.now(timezone.utc)
    session.add(
        AiAnalysisSettings(
            id=1,
            enabled=True,
            unattended_consent=True,
            interval_minutes=60,
            last_run_at=now,
            last_success_at=now - timedelta(hours=2),
            next_run_at=now + timedelta(minutes=FAILURE_BACKOFF_MINUTES),
        )
    )
    session.add(
        ChatGptCredential(
            id=1,
            client_id="oai",
            access_token_enc="enc",
            scopes_json=["chatgpt.tokens.use.direct"],
        )
    )
    await session.commit()
    for _ in range(5):
        assert await should_run_scheduled(session) is False


@pytest.mark.asyncio
async def test_token_failure_persisted_in_history(session):
    run = await run_ai_analysis(trigger="test")
    assert run.status == "failed"
    assert run.error_code == "not_connected"
    stored = (await session.execute(select(AiAnalysisRun).where(AiAnalysisRun.id == run.id))).scalar_one()
    assert stored.status == "failed"


def test_split_ai_telegram_report_preserves_full_long_body():
    body = "ف" * 5000
    parts = split_ai_telegram_report_messages(
        decision="NO_TRADE",
        report_fa=body,
        validation={"ok": True},
    )
    assert len(parts) >= 2
    assert all(len(p) <= 4096 for p in parts)
    assert "".join(parts).endswith("ف")
    assert body in "".join(parts)
    assert parts[0].startswith("تحلیل ICT — عدم ورود")


@pytest.mark.asyncio
async def test_telegram_ai_plain_payload_and_delivery():
    text = format_ai_telegram_message(
        decision="LONG",
        report_fa="متن با _underscore_ و *star* و `tick` [bracket]",
        validation={"ok": False, "reasons": ["x"]},
    )
    captured: dict = {}

    async def fake_post(url, json=None, **_kwargs):
        captured["payload"] = json
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})

    with patch("app.services.telegram_notifier.telegram_integration_active", return_value=True):
        with patch("app.services.telegram_notifier.get_effective_settings") as gs:
            settings = gs.return_value
            settings.telegram_bot_token = "123456789:abc"
            settings.telegram_chat_id = "1"
            with patch("app.services.telegram_notifier.httpx.AsyncClient") as client_cls:
                client_cls.return_value.__aenter__.return_value.post = fake_post
                msg_id = await TelegramNotifier().send_ai_report(text)
    assert msg_id == 99
    assert "parse_mode" not in captured["payload"]
    assert "سیگنال تأیید نشده" in captured["payload"]["text"]
