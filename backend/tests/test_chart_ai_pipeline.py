import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.domain.candles import Candle
from app.models import AiAnalysisRun, ChatGptCredential
from app.services.ai_ict_analysis import run_ai_analysis
from app.services.chart_media import chart_path_for_run, charts_dir
from app.services.chart_renderer import ChartRenderError, render_ict_chart_png
from app.services.siwc.credentials import get_access_token
from app.services.siwc.oauth import JWKS_URL
from app.services.telegram_notifier import TelegramNotifier


def _synthetic_candles(n: int = 80, start_price: float = 100_000.0) -> list[Candle]:
    base_ms = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    candles: list[Candle] = []
    price = start_price
    for i in range(n):
        o = price
        c = price + (1 if i % 3 else -1) * 25
        h = max(o, c) + 40
        l = min(o, c) - 40
        candles.append(Candle(open=o, high=h, low=l, close=c, time_ms=base_ms + i * 900_000))
        price = c
    return candles


def test_jwks_url_well_known():
    assert JWKS_URL == "https://auth.openai.com/.well-known/jwks.json"


@pytest.mark.asyncio
async def test_credentials_accepts_naive_expiry(session):
    naive_future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=2)
    session.add(
        ChatGptCredential(
            id=1,
            client_id="oai",
            access_token_enc="enc:token",
            scopes_json=["chatgpt.tokens.use.direct"],
            access_expires_at=naive_future,
        )
    )
    await session.commit()
    with patch("app.services.siwc.credentials.decrypt_value", return_value="token"):
        token, _, err = await get_access_token(session)
    assert err is None
    assert token == "token"


def test_chart_renderer_long_short_and_no_trade(tmp_path):
    candles = _synthetic_candles()
    overlays = {
        "bos": "BULLISH",
        "mss": None,
        "liquidity_levels": [candles[-1].close - 200],
        "fvg_zones": [
            {
                "low": candles[-5].low,
                "high": candles[-5].low + 120,
                "direction": "BULLISH",
                "time_ms": candles[-5].time_ms,
            }
        ],
        "liquidity_sweep": {"time_ms": candles[-10].time_ms, "level": candles[-10].low},
    }
    for decision, levels in (
        ("LONG", {"entry": candles[-1].close, "stop_loss": candles[-1].close - 500, "take_profit": candles[-1].close + 900}),
        ("SHORT", {"entry": candles[-1].close, "stop_loss": candles[-1].close + 500, "take_profit": candles[-1].close - 900}),
        ("NO_TRADE", None),
    ):
        out = tmp_path / f"{decision}.png"
        render_ict_chart_png(
            output_path=out,
            symbol="BTCUSDT",
            interval="15m",
            candles=candles,
            overlays=overlays,
            live_price=candles[-1].close,
            decision=decision,
            trade_levels=levels,
            analyzed_at=datetime.now(timezone.utc),
        )
        assert out.stat().st_size > 8_000


def test_chart_renderer_rejects_bad_data(tmp_path):
    with pytest.raises(ChartRenderError):
        render_ict_chart_png(
            output_path=tmp_path / "bad.png",
            symbol="BTCUSDT",
            interval="15m",
            candles=[],
            overlays={},
            live_price=0,
            decision="NO_TRADE",
            trade_levels=None,
            analyzed_at=datetime.now(timezone.utc),
        )


@pytest.mark.asyncio
async def test_ai_pipeline_chart_and_telegram(session, client, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    charts_dir()

    cred = ChatGptCredential(
        id=1,
        client_id="oai",
        access_token_enc="enc",
        scopes_json=["chatgpt.tokens.use.direct"],
        preferred_model="m",
        access_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    session.add(cred)
    await session.commit()

    candles = _synthetic_candles(60)
    ctx = {
        "symbol": "BTCUSDT",
        "live_price": candles[-1].close,
        "timeframes": {
            "15m": {
                "candles": [
                    {"time_ms": c.time_ms, "open": c.open, "high": c.high, "low": c.low, "close": c.close}
                    for c in candles
                ],
                "overlays": {"bos": "BULLISH", "mss": None, "liquidity_levels": [], "fvg_zones": []},
            }
        },
    }
    ai_json = {
        "decision": "LONG",
        "entry_price": candles[-1].close,
        "stop_loss": candles[-1].close - 400,
        "take_profit": candles[-1].close + 800,
        "report_fa": "گزارش تست",
    }
    posts: list[dict] = []

    async def fake_post(url, json=None, data=None, files=None, **_kwargs):
        posts.append({"url": url, "json": json, "data": data, "files": bool(files)})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(posts)}})

    with patch("app.services.ai_ict_analysis.get_access_token", AsyncMock(return_value=("tok", cred, None))):
        with patch("app.services.ai_ict_analysis.build_market_context", AsyncMock(return_value=(ctx, candles[-1].close))):
            with patch(
                "app.services.ai_ict_analysis.stream_response",
                AsyncMock(return_value=f"```json\n{json.dumps(ai_json)}\n```"),
            ):
                with patch("app.services.telegram_notifier.telegram_integration_active", return_value=True):
                    with patch("app.services.telegram_notifier.get_effective_settings") as gs:
                        settings = gs.return_value
                        settings.telegram_bot_token = "123456789:abc"
                        settings.telegram_chat_id = "1"
                        with patch("app.services.telegram_notifier.httpx.AsyncClient") as client_cls:
                            client_cls.return_value.__aenter__.return_value.post = fake_post
                            run = await run_ai_analysis(trigger="manual")

    assert run.status.startswith("completed")
    assert run.telegram_sent is True
    art = (run.structured or {}).get("_artifacts") or {}
    assert art.get("chart", {}).get("filename")
    assert chart_path_for_run(run.id).is_file()
    assert any(p["files"] for p in posts)
    assert any(p["json"] and "text" in (p["json"] or {}) for p in posts)

    resp = await client.get(f"/api/gui/chatgpt/history/{run.id}/chart")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")


@pytest.mark.asyncio
async def test_chart_failure_still_sends_text(session, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(Path("/tmp/tradeonbu-chart-fail-test")))
    cred = ChatGptCredential(
        id=1,
        client_id="oai",
        access_token_enc="enc",
        scopes_json=["chatgpt.tokens.use.direct"],
        preferred_model="m",
        access_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    session.add(cred)
    await session.commit()

    ctx = {"symbol": "BTCUSDT", "timeframes": {"15m": {"candles": [], "overlays": {}}}}
    ai_json = {"decision": "NO_TRADE", "report_fa": "بدون معامله"}

    async def fake_post(url, json=None, **_kwargs):
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    with patch("app.services.ai_ict_analysis.get_access_token", AsyncMock(return_value=("tok", cred, None))):
        with patch("app.services.ai_ict_analysis.build_market_context", AsyncMock(return_value=(ctx, 100.0))):
            with patch(
                "app.services.ai_ict_analysis.stream_response",
                AsyncMock(return_value=f"```json\n{json.dumps(ai_json)}\n```"),
            ):
                with patch("app.services.ai_ict_analysis.ChartDataService") as chart_cls:
                    chart_cls.return_value.bundle = AsyncMock(side_effect=RuntimeError("market down"))
                    with patch("app.services.telegram_notifier.telegram_integration_active", return_value=True):
                        with patch("app.services.telegram_notifier.get_effective_settings") as gs:
                            settings = gs.return_value
                            settings.telegram_bot_token = "123456789:abc"
                            settings.telegram_chat_id = "1"
                            with patch("app.services.telegram_notifier.httpx.AsyncClient") as client_cls:
                                client_cls.return_value.__aenter__.return_value.post = fake_post
                                run = await run_ai_analysis(trigger="manual")

    assert run.status.startswith("completed")
    assert run.telegram_sent is True
    chart_art = ((run.structured or {}).get("_artifacts") or {}).get("chart") or {}
    assert chart_art.get("error")


@pytest.mark.asyncio
async def test_send_photo_failure_does_not_block_text():
    calls = {"n": 0}

    async def fake_post(url, json=None, data=None, files=None, **_kwargs):
        calls["n"] += 1
        if files:
            return httpx.Response(500, json={"ok": False})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    path = Path("/tmp/chart-photo-fail.png")
    path.write_bytes(b"not-a-real-png")

    with patch("app.services.telegram_notifier.telegram_integration_active", return_value=True):
        with patch("app.services.telegram_notifier.get_effective_settings") as gs:
            settings = gs.return_value
            settings.telegram_bot_token = "123456789:abc"
            settings.telegram_chat_id = "1"
            with patch("app.services.telegram_notifier.httpx.AsyncClient") as client_cls:
                client_cls.return_value.__aenter__.return_value.post = fake_post
                notifier = TelegramNotifier()
                photo_id = await notifier.send_photo_file(path, "cap")
                text_id = await notifier.send_ai_report("body")
    assert photo_id is None
    assert text_id == 42
