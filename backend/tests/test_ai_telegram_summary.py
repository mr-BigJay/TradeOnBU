from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services.ai_ict_analysis import _RunDraft, _deliver_ai_telegram
from app.services.ai_telegram_summary import build_ai_telegram_summary
from app.services.telegram_message_format import strip_telegram_bidi_control_chars

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def sample():
    return dict(final_ai_decision="WAIT", setup_mode="conditional", potential_direction="LONG",
                strategy_id="ict_reversal", market_regime="bearish", trade_against_dominant_trend=True,
                entry_price=77247.85, entry_zone_low=77217.9, entry_zone_high=77277.8,
                stop_loss=76750, take_profit=79765.3,
                ict_structure="ساختار نزولی است؛ MSS هنوز تأیید نشده.",
                confirmation_required="جاروب، شکست ساختار و بازآزمایی ناحیه لازم است.",
                conditional_invalidation="بسته‌شدن زیر 76750")


def test_summary_preserves_levels_wait_and_does_not_mutate_analysis():
    s = sample()
    original = deepcopy(s)
    text = build_ai_telegram_summary(symbol="BTCUSDT", live=77387.5, decision="NO_TRADE",
                                    structured=s, validation={"ok": True}, report_fa="full report", analyzed_at=NOW)
    plain = strip_telegram_bidi_control_chars(text)
    for value in ("77,247.85", "77,217.9", "77,277.8", "76,750", "79,765.3", "1:2.5",
                  "انتظار", "خلاف روند", "شرط ورود", "ابطال"):
        assert value in plain
    assert "\u20661:2.5\u2069" in text
    assert s == original
    assert len(text.encode('utf-16-le')) // 2 <= 1024


@pytest.mark.parametrize("decision", ["LONG", "SHORT", "NO_TRADE", "WAIT"])
def test_long_unicode_summary_fits_single_caption(decision):
    s = sample()
    s.update(final_ai_decision=decision, setup_mode="immediate")
    for key in ("ict_structure", "confirmation_required", "conditional_invalidation"):
        s[key] = "متن طولانی 🚀 77387.5 BTCUSDT " * 1000
    text = build_ai_telegram_summary(symbol="BTCUSDT", live=77387.5, decision=decision,
                                    structured=s, validation={"ok": False, "reasons": ["رد"]},
                                    report_fa="full", analyzed_at=NOW)
    assert len(text.encode('utf-16-le')) // 2 <= 1024
    assert text.count('\u2066') == text.count('\u2069')
    if decision in ("LONG", "SHORT"):
        assert "ورود مجاز نیست" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("photo_result,chart_exists", [(42, True), (None, True), (None, False)])
async def test_delivery_one_summary_and_original_report_unchanged(tmp_path, photo_result, chart_exists):
    chart = tmp_path / 'chart.png'
    if chart_exists:
        chart.write_bytes(b'fake photo')
    s = sample()
    report = "گزارش کامل " * 1000
    draft = _RunDraft(decision="NO_TRADE", structured=deepcopy(s), validation={"ok": True}, report_fa=report)
    with patch('app.services.ai_ict_analysis.TelegramNotifier') as cls:
        notifier = cls.return_value
        notifier.enabled = True
        notifier.send_photo_file = AsyncMock(return_value=photo_result)
        notifier.send_ai_report = AsyncMock(return_value=43)
        await _deliver_ai_telegram(draft=draft, symbol="BTCUSDT", live=77387.5,
                                   chart_path=chart, analyzed_at=NOW)
        assert notifier.send_photo_file.await_count == int(chart_exists)
        if photo_result is not None:
            notifier.send_ai_report.assert_not_awaited()
            caption = notifier.send_photo_file.call_args.args[1]
        else:
            notifier.send_ai_report.assert_awaited_once()
            caption = notifier.send_ai_report.call_args.args[0]
        assert len(caption.encode('utf-16-le')) // 2 <= 1024
    assert draft.telegram_sent
    assert draft.report_fa == report
    for key in s:
        assert draft.structured[key] == s[key]
