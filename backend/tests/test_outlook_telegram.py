from types import SimpleNamespace
from datetime import datetime,timezone
from app.services.outlook_telegram import format_outlook_message
from tests.test_daily_outlook import report


def row(plan):
    r=report();r['trade_plan']=plan
    return SimpleNamespace(id=21,symbol='BTCUSDT',slot_close_ms=1,context={'live_price':75946.9},report=r)


def test_wait_never_invents_execution_levels():
    r=row({'action':'wait'})
    text=format_outlook_message(r,datetime.now(timezone.utc),'https://example.test')
    assert 'انتظار؛ ورود تأیید نشده' in text
    assert 'ورود پیشنهادی:' not in text and '1:2.5' not in text
    assert '/#/analysis/reports/21' in text
    assert 'شرط فعال‌شدن سناریو:' in text and 'ابطال سناریو:' in text
    assert len(text)<4000


def test_only_engine_prices_shown_with_actual_net_ratio():
    plan={'source':'ict_engine','action':'long','entry_price':75000,'stop_loss':74500,'take_profits':[75800],'risk_reward':[1.41]}
    r=row(plan)
    text=format_outlook_message(r,datetime.now(timezone.utc),'https://example.test')
    assert '75,000' in text and '74,500' in text and '75,800' in text
    assert '1:1.41' in text and '1:2.5' not in text
    r.report['trade_plan']['source']='ai'
    assert 'ورود پیشنهادی:' not in format_outlook_message(r,datetime.now(timezone.utc),'')
