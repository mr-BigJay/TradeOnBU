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
from app.services.chart_renderer import (
    ChartRenderError,
    _fvg_zone_color,
    _sanitize_candles,
    _candles_to_df,
    apply_candle_time_axis,
    format_candle_axis_tick,
    render_ict_chart_png,
)
from app.services.ict_structure import build_chart_overlays
from app.services.siwc.credentials import get_access_token
from app.services.siwc.oauth import JWKS_URL
from app.services.siwc.responses_client import AnalysisResponse
from app.services.telegram_notifier import TelegramNotifier


def _analysis_response_for_json(ai_json: dict) -> AnalysisResponse:
    return AnalysisResponse(
        text=f"```json\n{json.dumps(ai_json)}\n```",
        requested_model="gpt-6.1-sol",
        requested_reasoning_effort="max",
        response_model="gpt-6.1-sol",
    )


def _candles_from_ctx(ctx: dict) -> list[Candle]:
    rows = (ctx.get("timeframes") or {}).get("15m", {}).get("candles") or []
    return [
        Candle(
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            time_ms=int(r["time_ms"]),
        )
        for r in rows
    ]


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


def test_charts_dir_uses_data_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "app_data"))
    path = charts_dir()
    assert path == tmp_path / "app_data" / "media" / "charts"
    path.mkdir(parents=True, exist_ok=True)
    assert path.is_dir()


def test_candle_time_axis_tick_maps_integer_positions():
    candles = _synthetic_candles(24)
    clean = _sanitize_candles(candles)
    df = _candles_to_df(clean)
    expected = datetime.fromtimestamp(clean[0].time_ms / 1000.0, tz=timezone.utc).strftime("%m-%d %H:%M")
    assert format_candle_axis_tick(df.index, 0.0, 0.0) == expected
    last_expected = datetime.fromtimestamp(clean[-1].time_ms / 1000.0, tz=timezone.utc).strftime("%m-%d %H:%M")
    assert format_candle_axis_tick(df.index, float(len(df) - 1), 0.0) == last_expected


def test_candle_time_axis_regression_on_mplfinance_integer_x():
    import matplotlib.pyplot as plt

    candles = _synthetic_candles(40)
    df = _candles_to_df(_sanitize_candles(candles))
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.set_xlim(-0.5, len(df) - 0.5)
    apply_candle_time_axis(ax, df.index, max_ticks=6)
    fig.canvas.draw()
    labels = [t.get_text() for t in ax.get_xticklabels() if t.get_text()]
    plt.close(fig)
    assert labels
    anchor = datetime.fromtimestamp(candles[0].time_ms / 1000.0, tz=timezone.utc).strftime("%m-%d")
    assert any(anchor in label for label in labels)


def _candles_with_bullish_fvg() -> list[Candle]:
    base_ms = int(datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    candles: list[Candle] = []
    price = 50_000.0
    for i in range(32):
        o = price
        c = price + 20
        candles.append(Candle(open=o, high=o + 80, low=o - 80, close=c, time_ms=base_ms + i * 900_000))
        price = c
    i = len(candles) - 1
    c0 = candles[i - 2]
    c1 = candles[i - 1]
    candles[i - 2] = Candle(open=c0.open, high=50_100.0, low=49_950.0, close=50_000.0, time_ms=c0.time_ms)
    candles[i] = Candle(
        open=50_450.0,
        high=50_550.0,
        low=50_400.0,
        close=50_500.0,
        time_ms=c1.time_ms + 900_000,
    )
    return candles


def test_fvg_overlay_direction_long_short_from_ict_structure():
    candles = _candles_with_bullish_fvg()
    overlays = build_chart_overlays(candles, direction="LONG")
    zones = overlays.get("fvg_zones") or []
    assert zones
    directions = {str(z["direction"]).upper() for z in zones}
    assert directions.issubset({"LONG", "SHORT"})
    for direction in directions:
        assert _fvg_zone_color(direction) in ("#a78bfa", "#fb7185")
    assert _fvg_zone_color("LONG") == _fvg_zone_color("BULLISH")
    assert _fvg_zone_color("SHORT") == _fvg_zone_color("BEARISH")


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
                "direction": "LONG",
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
        with patch(
            "app.services.ai_ict_analysis.build_raw_market_payload",
            AsyncMock(return_value=(ctx, candles[-1].close, {"15m": candles})),
        ):
            with patch("app.services.ai_ict_analysis.build_ai_memory_context", AsyncMock(return_value={"open_setups": []})):
                with patch(
                    "app.services.ai_ict_analysis.stream_analysis_response",
                    AsyncMock(return_value=_analysis_response_for_json(ai_json)),
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
    assert len(posts) == 1
    assert posts[0]["url"].endswith("/sendPhoto")
    assert "هدف تحلیلی" in posts[0]["data"]["caption"]
    assert len(posts[0]["data"]["caption"].encode("utf-16-le")) // 2 <= 1024

    resp = await client.get(f"/api/gui/chatgpt/history/{run.id}/chart")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    disposition = resp.headers.get("content-disposition", "")
    assert "inline" in disposition.lower()


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
        with patch(
            "app.services.ai_ict_analysis.build_raw_market_payload",
            AsyncMock(return_value=(ctx, 100.0, {"15m": []})),
        ):
            with patch("app.services.ai_ict_analysis.build_ai_memory_context", AsyncMock(return_value={"open_setups": []})):
                with patch(
                    "app.services.ai_ict_analysis.stream_analysis_response",
                    AsyncMock(return_value=_analysis_response_for_json(ai_json)),
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
    chart_art = ((run.structured or {}).get("_artifacts") or {}).get("chart") or {}
    assert chart_art.get("error")


@pytest.mark.asyncio
async def test_unexpected_chart_render_exception_still_completes_and_sends_text(session, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(Path("/tmp/tradeonbu-chart-unexpected")))
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
    candles = _synthetic_candles(50)
    ctx = {
        "symbol": "BTCUSDT",
        "timeframes": {
            "15m": {
                "candles": [
                    {"time_ms": c.time_ms, "open": c.open, "high": c.high, "low": c.low, "close": c.close}
                    for c in candles
                ],
                "overlays": {},
            }
        },
    }
    ai_json = {"decision": "NO_TRADE", "report_fa": "گزارش پایدار"}

    async def fake_post(url, json=None, **_kwargs):
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    with patch("app.services.ai_ict_analysis.get_access_token", AsyncMock(return_value=("tok", cred, None))):
        with patch(
            "app.services.ai_ict_analysis.build_raw_market_payload",
            AsyncMock(return_value=(ctx, candles[-1].close, {"15m": candles})),
        ):
            with patch("app.services.ai_ict_analysis.build_ai_memory_context", AsyncMock(return_value={"open_setups": []})):
                with patch(
                    "app.services.ai_ict_analysis.stream_analysis_response",
                    AsyncMock(return_value=_analysis_response_for_json(ai_json)),
                ):
                    with patch(
                        "app.services.ai_ict_analysis.render_ict_chart_png",
                        side_effect=OSError("disk full"),
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
    assert run.report_fa.startswith("تحلیل ICT برای BTCUSDT")
    assert "تصمیم:" in run.report_fa
    assert "عدم ورود (NO_TRADE)" in run.report_fa
    chart_art = ((run.structured or {}).get("_artifacts") or {}).get("chart") or {}
    assert chart_art.get("error")


@pytest.mark.asyncio
async def test_chart_is_file_oserror_still_sends_text(session, monkeypatch, tmp_path):
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
    candles = _synthetic_candles(50)
    ctx = {
        "symbol": "BTCUSDT",
        "timeframes": {
            "15m": {
                "candles": [
                    {"time_ms": c.time_ms, "open": c.open, "high": c.high, "low": c.low, "close": c.close}
                    for c in candles
                ],
                "overlays": {},
            }
        },
    }
    ai_json = {"decision": "NO_TRADE", "report_fa": "متن بعد از خطای is_file"}

    async def fake_post(url, json=None, **_kwargs):
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 11}})

    real_is_file = Path.is_file

    def broken_is_file(self):
        if "media/charts/run_" in str(self).replace("\\", "/"):
            raise OSError("stat failed")
        return real_is_file(self)

    with patch("app.services.ai_ict_analysis.get_access_token", AsyncMock(return_value=("tok", cred, None))):
        with patch(
            "app.services.ai_ict_analysis.build_raw_market_payload",
            AsyncMock(return_value=(ctx, candles[-1].close, {"15m": candles})),
        ):
            with patch("app.services.ai_ict_analysis.build_ai_memory_context", AsyncMock(return_value={"open_setups": []})):
                with patch(
                    "app.services.ai_ict_analysis.stream_analysis_response",
                    AsyncMock(return_value=_analysis_response_for_json(ai_json)),
                ):
                    with patch.object(Path, "is_file", broken_is_file):
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
    tg = ((run.structured or {}).get("_artifacts") or {}).get("telegram") or {}
    assert tg.get("photo_error")
    assert tg.get("text_message_id") == 11


@pytest.mark.asyncio
async def test_unexpected_photo_upload_exception_still_sends_text(session, monkeypatch, tmp_path):
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
    candles = _synthetic_candles(50)
    ctx = {
        "symbol": "BTCUSDT",
        "timeframes": {
            "15m": {
                "candles": [
                    {"time_ms": c.time_ms, "open": c.open, "high": c.high, "low": c.low, "close": c.close}
                    for c in candles
                ],
                "overlays": {},
            }
        },
    }
    ai_json = {"decision": "NO_TRADE", "report_fa": "متن بعد از خطای عکس"}

    async def boom_photo(self, photo_path, caption):
        raise RuntimeError("upload exploded")

    async def fake_post(url, json=None, **_kwargs):
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    with patch("app.services.ai_ict_analysis.get_access_token", AsyncMock(return_value=("tok", cred, None))):
        with patch(
            "app.services.ai_ict_analysis.build_raw_market_payload",
            AsyncMock(return_value=(ctx, candles[-1].close, {"15m": candles})),
        ):
            with patch("app.services.ai_ict_analysis.build_ai_memory_context", AsyncMock(return_value={"open_setups": []})):
                with patch(
                    "app.services.ai_ict_analysis.stream_analysis_response",
                    AsyncMock(return_value=_analysis_response_for_json(ai_json)),
                ):
                    with patch("app.services.telegram_notifier.telegram_integration_active", return_value=True):
                        with patch("app.services.telegram_notifier.get_effective_settings") as gs:
                            settings = gs.return_value
                            settings.telegram_bot_token = "123456789:abc"
                            settings.telegram_chat_id = "1"
                            with patch.object(TelegramNotifier, "send_photo_file", boom_photo):
                                with patch("app.services.telegram_notifier.httpx.AsyncClient") as client_cls:
                                    client_cls.return_value.__aenter__.return_value.post = fake_post
                                    run = await run_ai_analysis(trigger="manual")

    assert run.status.startswith("completed")
    assert run.telegram_sent is True
    tg = ((run.structured or {}).get("_artifacts") or {}).get("telegram") or {}
    assert tg.get("photo_error")
    assert tg.get("text_message_id") == 9


def test_telegram_retry_after_malformed_header():
    resp = httpx.Response(429, headers={"Retry-After": "not-a-number"})
    assert TelegramNotifier._retry_after_seconds(resp) == 0.0


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
