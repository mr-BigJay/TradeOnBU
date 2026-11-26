from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
import pytest
from app.domain.candles import Candle
from app.models import AiAnalysisRun, HourlyReaction
from app.services import hourly_reaction as hourly
from tests.conftest import TestSession


@pytest.mark.asyncio
async def test_ai_is_asked_even_when_price_is_far_from_zone(session, monkeypatch):
    close = datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0)
    slot = int(close.timestamp()*1000)
    plan = {"strategy":"A","direction":"LONG","zone_low":80,"zone_high":85}
    session.add(AiAnalysisRun(status="completed",completed_at=close-timedelta(hours=2),
        structured={"analyst_summary":{"hourly_watch":{"A:LONG:1":plan}}}))
    session.add(HourlyReaction(close_ms=slot))
    await session.commit()
    ask = AsyncMock(return_value={"action":"WAIT","reason_fa":"ناحیه لمس نشد"})
    monkeypatch.setattr(hourly,"SessionLocal",TestSession)
    monkeypatch.setattr(hourly,"ask",ask)
    await hourly.review_close(slot,[Candle(200,202,198,201,slot-hourly.HOUR)])
    assert ask.await_args.args[1]["watch"]["A:LONG:1"] == plan
    await session.rollback()
    row = await session.get(HourlyReaction,slot)
    assert row.status == "wait"
    assert row.notice is None
