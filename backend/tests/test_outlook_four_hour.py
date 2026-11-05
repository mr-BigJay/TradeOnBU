"""Closed-candle scheduling, migration and independent Telegram reporting."""
import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, text, select, func
from sqlalchemy.exc import IntegrityError
from app.db import _migrate_outlook_slots
from app.models import DailyOutlookRun, DailyOutlookSettings
from app.services import daily_outlook as service
from tests.conftest import TestSession
from tests.test_daily_outlook import context, report, isolated


class Clock(datetime):
    value = datetime(2026, 11, 5, 4, 5, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)


@pytest.fixture
def four(monkeypatch, isolated):
    monkeypatch.setenv('OUTLOOK_FOUR_HOUR_ENABLED', '1')
    monkeypatch.setattr(service, 'datetime', Clock)
    Clock.value = datetime(2026, 11, 5, 4, 5, tzinfo=timezone.utc)


@pytest.mark.parametrize('hour', [0, 4, 8, 12, 16, 20])
def test_slots_are_utc_even_with_tehran_input(hour):
    close = datetime(2026, 11, 5, hour, tzinfo=timezone.utc)
    assert service.four_hour_slot(close.astimezone(service.TEHRAN)) == (close, close + timedelta(minutes=5))


@pytest.mark.asyncio
async def test_guard_six_slots_duplicate_and_disabled(four):
    for hour in [0, 4, 8, 12, 16, 20]:
        Clock.value = datetime(2026, 11, 5, hour, 4, 59, tzinfo=timezone.utc)
        assert (await service.reserve_run('scheduled'))[2] == 'waiting_for_closed_4h'
        assert not (await service.dashboard())['can_generate']
        Clock.value += timedelta(seconds=1)
        rid, owner, state = await service.reserve_run('scheduled')
        assert rid and owner and state == 'started'
        assert (await service.reserve_run('scheduled'))[2] == 'already_attempted_today'
    async with TestSession() as s:
        assert await s.scalar(select(func.count()).select_from(DailyOutlookRun)) == 6
        cfg = await s.get(DailyOutlookSettings, 1)
        cfg.enabled = False
        await s.commit()
    Clock.value += timedelta(hours=4)
    assert (await service.reserve_run('scheduled'))[2] == 'not_due'
    assert not (await service.dashboard())['settings']['enabled']


@pytest.mark.asyncio
async def test_busy_retries_within_window_no_late_backfill(four):
    service.acquire_analysis_lock.return_value = False
    assert (await service.reserve_run('scheduled'))[2] == 'analysis_busy'
    Clock.value += timedelta(minutes=3)
    service.acquire_analysis_lock.return_value = True
    assert (await service.reserve_run('scheduled'))[2] == 'started'
    Clock.value += timedelta(hours=4, minutes=12)
    assert (await service.reserve_run('scheduled'))[2] == 'not_due'
    assert (await service.dashboard())['settings']['next_run_at'].startswith('2026-11-05T12:05')


def test_old_sqlite_history_survives_atomic_migration_and_unique_slots(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path}/old.db')
    with engine.begin() as c:
        c.execute(text('''CREATE TABLE daily_outlook_runs (
            id INTEGER PRIMARY KEY, symbol VARCHAR(32) NOT NULL, report_date VARCHAR(10) NOT NULL,
            trigger VARCHAR(16) NOT NULL, status VARCHAR(16) NOT NULL, started_at DATETIME NOT NULL,
            completed_at DATETIME, model_slug VARCHAR(80), context JSON NOT NULL,
            report JSON NOT NULL, usage JSON NOT NULL, error_code VARCHAR(80),
            CONSTRAINT uq_daily_outlook_symbol_date UNIQUE(symbol, report_date))'''))
        c.execute(text("INSERT INTO daily_outlook_runs VALUES (14,'BTCUSDT','2026-11-05','manual','completed','2026-11-05 05:35:00',NULL,NULL,'{}','{\"summary_fa\":\"saved\"}','{}',NULL)"))
    with engine.begin() as c:
        _migrate_outlook_slots(c)
    with engine.begin() as c:
        _migrate_outlook_slots(c)
        saved = c.execute(text('SELECT id, report, slot_close_ms FROM daily_outlook_runs')).one()
        assert saved == (14, '{"summary_fa":"saved"}', 0)
        for slot in [14400000, 28800000]:
            c.execute(text('INSERT INTO daily_outlook_runs (symbol,report_date,slot_close_ms,trigger,status,started_at,context,report,usage) SELECT symbol,report_date,:slot,trigger,status,started_at,context,report,usage FROM daily_outlook_runs WHERE id=14'), {'slot': slot})
        assert c.scalar(text('SELECT count(*) FROM daily_outlook_runs')) == 3
        with pytest.raises(IntegrityError):
            c.execute(text('INSERT INTO daily_outlook_runs (symbol,report_date,slot_close_ms,trigger,status,started_at,context,report,usage) SELECT symbol,report_date,14400000,trigger,status,started_at,context,report,usage FROM daily_outlook_runs WHERE id=14'))
    engine.dispose()


@pytest.mark.asyncio
async def test_missing_latest_candle_blocks_paid_call(four, monkeypatch):
    rid, owner, _ = await service.reserve_run('scheduled')
    monkeypatch.setattr(service, 'build_daily_context', AsyncMock(return_value=context()))
    ai = AsyncMock()
    monkeypatch.setattr(service, 'stream_analysis_response', ai)
    await service.execute_reserved(rid, owner)
    ai.assert_not_awaited()
    assert (await service.dashboard())['latest']['error_code'] == 'latest_closed_candle_missing'


@pytest.mark.asyncio
@pytest.mark.parametrize('send_result', [1234, None])
async def test_report_without_candidate_sends_once_and_keeps_completed(four, monkeypatch, send_result):
    rid, owner, _ = await service.reserve_run('scheduled')
    ctx = context()
    slot = int(service.four_hour_slot(Clock.value)[0].timestamp()*1000)
    ctx['candles'] = {'4h': [[slot-14400000,100,110,90,105]]}
    monkeypatch.setattr(service, 'build_daily_context', AsyncMock(return_value=ctx))
    monkeypatch.setattr(service, 'snapshot_plan', AsyncMock(return_value={'source':'ict_engine','action':'wait','rationale_fa':'بدون ستاپ','trigger_fa':'انتظار'}))
    monkeypatch.setattr(service, 'load_ai_market_intelligence', AsyncMock(return_value={}))
    monkeypatch.setattr(service, 'provider_credentials', AsyncMock(return_value=('test','test-model','high')))
    ai = AsyncMock(return_value=SimpleNamespace(text=json.dumps(report()), response_metadata={'usage':{'input_tokens':10}}))
    monkeypatch.setattr(service, 'stream_analysis_response', ai)
    from app.services import telegram_notifier
    send = AsyncMock(return_value=send_result)
    monkeypatch.setattr(telegram_notifier, 'TelegramNotifier', lambda: SimpleNamespace(enabled=True, send_message=send, settings=SimpleNamespace(dashboard_public_url='https://example.test')))
    await service.execute_reserved(rid, owner)
    latest = (await service.dashboard())['latest']
    assert latest['status'] == 'completed'
    assert latest['context']['telegram_delivery']['status'] == ('sent' if send_result else 'failed_or_unknown')
    assert latest['report']['trade_plan']['action'] == 'wait'
    await service.deliver_outlook(rid)
    send.assert_awaited_once()
    ai.assert_awaited_once()
    assert 'شرط فعال‌شدن' in send.call_args.args[0]

@pytest.mark.asyncio
async def test_tehran_midnight_does_not_duplicate_same_utc_slot(four):
    Clock.value = datetime(2026, 11, 5, 20, 5, tzinfo=timezone.utc)
    rid, _, _ = await service.reserve_run('scheduled')
    Clock.value += timedelta(hours=1)
    assert (await service.reserve_run('manual')) == (rid, None, 'already_attempted_today')
    assert (await service.dashboard())['attempted_today']
