import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.models import AiAnalysisSettings, ChatGptCredential
from app.services.ai_analysis_scheduler import (
    DEFAULT_AI_INTERVAL_MINUTES,
    migrate_ai_schedule_settings,
    record_run_outcome,
    should_run_scheduled,
)
from app.services.ai_setup_memory import apply_objective_price_tracking, create_setup_from_structured
from app.services.siwc.responses_client import OpenAIResponsesError, stream_analysis_response
from app.domain.candles import Candle


@pytest.mark.asyncio
async def test_stream_analysis_payload_includes_reasoning_max():
    captured: dict = {}

    class FakeResp:
        status_code = 200

        async def aread(self):
            return b""

        async def aiter_lines(self):
            yield 'data: {"type":"response.output_text.delta","delta":"{}"}\n\n'
            yield (
                'data: {"type":"response.completed","response":{"model":"gpt-6.1-sol","id":"resp_1",'
                '"reasoning":{"effort":"max"}}}\n\n'
            )

    class StreamCtx:
        async def __aenter__(self):
            return FakeResp()

        async def __aexit__(self, *_args):
            return None

    def capture_stream(_method, _url, **kwargs):
        captured["json"] = kwargs.get("json")
        return StreamCtx()

    mock_client = MagicMock()
    mock_client.stream = capture_stream
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_client)
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.siwc.responses_client.httpx.AsyncClient", return_value=cm):
        result = await stream_analysis_response(
            "tok",
            model="gpt-6.1-sol",
            instructions="i",
            user_input="u",
            reasoning_effort="max",
        )
    assert captured["json"]["model"] == "gpt-6.1-sol"
    assert captured["json"]["reasoning"] == {"effort": "max"}
    assert result.requested_reasoning_effort == "max"
    assert result.response_model == "gpt-6.1-sol"


@pytest.mark.asyncio
async def test_stream_analysis_rejects_reasoning_downgrade():
    class FakeResp:
        status_code = 200

        async def aread(self):
            return b""

        async def aiter_lines(self):
            yield (
                'data: {"type":"response.completed","response":{"model":"gpt-6.1-sol",'
                '"reasoning":{"effort":"high"}}}\n\n'
            )

    class StreamCtx:
        async def __aenter__(self):
            return FakeResp()

        async def __aexit__(self, *_args):
            return None

    mock_client = MagicMock()
    mock_client.stream = lambda *_a, **_k: StreamCtx()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_client)
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.siwc.responses_client.httpx.AsyncClient", return_value=cm):
        with pytest.raises(OpenAIResponsesError) as exc:
            await stream_analysis_response("tok", model="gpt-6.1-sol", instructions="i", user_input="u")
    assert exc.value.code == "reasoning_downgraded"


def test_migrate_schedule_leaves_sixty_minute_installs_unchanged():
    nr = datetime.now(timezone.utc) + timedelta(minutes=20)
    cfg = AiAnalysisSettings(
        enabled=True,
        unattended_consent=True,
        interval_minutes=60,
        last_run_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        next_run_at=nr,
        interval_migrated_from_60=False,
    )
    assert migrate_ai_schedule_settings(cfg) is False
    assert cfg.interval_minutes == 60
    assert cfg.unattended_consent is True
    assert cfg.next_run_at == nr
    assert cfg.interval_migrated_from_60 is False


@pytest.mark.asyncio
async def test_failure_backoff_does_not_schedule_hourly_routine(session):
    cfg = AiAnalysisSettings(
        id=1,
        enabled=True,
        unattended_consent=True,
        interval_minutes=120,
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
    record_run_outcome(cfg, "failed", "reasoning_max_unavailable", trigger="scheduled")
    await session.commit()
    assert cfg.next_run_at <= datetime.now(timezone.utc) + timedelta(minutes=20)
    assert cfg.next_run_at > datetime.now(timezone.utc) + timedelta(minutes=10)
    assert await should_run_scheduled(session) is False


@pytest.mark.asyncio
async def test_setup_memory_persist_and_objective_touch(session):
    from tests.conftest import with_immediate_monitor_fields

    structured = with_immediate_monitor_fields(
        {
            "decision": "LONG",
            "entry_price": 100.0,
            "entry_zone_low": 99.5,
            "entry_zone_high": 100.5,
            "stop_loss": 98.0,
            "take_profit": 105.0,
            "proposed_setup": {"new_setup": True, "thesis_fa": "تست"},
        }
    )
    row, meta = await create_setup_from_structured(session, run_id=1, symbol="BTCUSDT", structured=structured)
    await session.commit()
    assert row is not None and meta["created"]
    candles = [
        Candle(open=100, high=100.4, low=99.8, close=100.2, time_ms=1),
        Candle(open=100, high=100.4, low=97.5, close=98.5, time_ms=2),
    ]
    apply_objective_price_tracking(row, candles)
    assert row.entry_touched is True
    assert row.sl_touched is True
    assert row.paper_fill_recorded is False
