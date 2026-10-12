from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.config import get_effective_settings
from app.db import SessionLocal
from app.models import AiAnalysisRun
from app.services.ai_analysis_scheduler import get_or_create_settings, record_run_outcome
from app.services.ai_telegram_format import format_ai_telegram_message
from app.services.chart_data import ChartDataService
from app.services.execution_model import ExecutionModel
from app.services.market_data import MarketDataService
from app.services.siwc.credentials import get_access_token
from app.services.siwc.responses_client import OpenAIResponsesError, list_models, stream_response
from app.services.telegram_notifier import TelegramNotifier

REQUIRED_INTERVALS = ("4h", "15m", "5m", "1m")
MIN_CANDLES = 20

INSTRUCTIONS = """You are an ICT market analyst for BTCUSDT paper trading only.
Respond in Persian (Farsi) with structured JSON inside a single ```json code block.
Never recommend live order execution. Output JSON fields:
decision (LONG|SHORT|NO_TRADE), bias, structure_summary, liquidity, sweep, mss_bos, displacement, fvg,
entry_price, stop_loss, take_profit, invalidation_level, net_rr_estimate, report_fa (full Persian narrative).
Use only the supplied candle/structure data; if uncertain use NO_TRADE."""


class MarketContextError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass
class _RunDraft:
    status: str = "failed"
    model_slug: str | None = None
    decision: str | None = None
    report_fa: str | None = None
    structured: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    telegram_sent: bool = False
    error_code: str | None = None
    error_detail: str | None = None
    completed_at: datetime | None = None


async def build_market_context(symbol: str) -> tuple[dict[str, Any], float]:
    settings = get_effective_settings()
    market = MarketDataService(settings)
    chart = ChartDataService(market)
    ctx: dict[str, Any] = {"symbol": symbol, "timeframes": {}, "paper_only": True}

    for interval in REQUIRED_INTERVALS:
        try:
            bundle = await market.fetch_kline_bundle(symbol, interval, 120)
        except Exception as exc:
            raise MarketContextError("market_fetch_failed", f"{interval}: {exc}") from exc
        if not bundle.data_complete:
            raise MarketContextError("incomplete_candles", f"{interval} history has gaps")
        closed = bundle.closed[-120:]
        if len(closed) < MIN_CANDLES:
            raise MarketContextError("insufficient_candles", f"{interval} has fewer than {MIN_CANDLES} closed candles")
        full = await chart.bundle(symbol, interval, limit=120)
        ctx["timeframes"][interval] = {
            "candles": (full.get("candles") or [])[-40:],
            "overlays": full.get("overlays"),
            "count": len(closed),
        }

    quote = await market.fetch_live_price(symbol)
    if market.is_quote_stale(quote):
        raise MarketContextError("stale_live_price", "Live Bitunix quote is stale or unavailable")
    ctx["live_price"] = quote.price
    ctx["quote_fetched_at"] = quote.fetched_at.isoformat()
    ctx["quote_stale"] = False
    return ctx, quote.price


def _extract_json(text: str) -> dict[str, Any]:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    raw = match.group(1) if match else text.strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    return json.loads(raw)


def _validate_levels(data: dict[str, Any], live: float) -> dict[str, Any]:
    decision = str(data.get("decision", "NO_TRADE")).upper()
    out: dict[str, Any] = {"ok": False, "reasons": []}
    if decision == "NO_TRADE":
        out["ok"] = True
        out["paper_action"] = "none"
        return out
    if decision not in ("LONG", "SHORT"):
        out["reasons"].append("invalid_decision")
        return out
    if live <= 0:
        out["reasons"].append("missing_live_price")
        return out
    try:
        entry = float(data["entry_price"])
        sl = float(data["stop_loss"])
        tp = float(data["take_profit"])
    except (KeyError, TypeError, ValueError):
        out["reasons"].append("missing_levels")
        return out
    drift = abs(entry - live) / live * 100
    if drift > 2.5:
        out["reasons"].append(f"entry_far_from_live:{drift:.2f}%")
    model = ExecutionModel()
    rr = model.calc_rr(decision, entry, sl, tp, "LIMIT", min_net_rr=get_effective_settings().min_net_rr)
    out["net_rr"] = rr.net_rr
    out["acceptable"] = rr.acceptable
    if not rr.acceptable:
        out["reasons"].append("net_rr_below_threshold")
    out["ok"] = len(out["reasons"]) == 0
    return out


async def _create_run_record(*, trigger: str) -> int:
    async with SessionLocal() as session:
        run = AiAnalysisRun(
            status="running",
            trigger=trigger,
            is_ai=True,
            started_at=datetime.now(timezone.utc),
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run.id


async def _persist_run_draft(run_id: int, draft: _RunDraft) -> AiAnalysisRun:
    completed = draft.completed_at or datetime.now(timezone.utc)
    async with SessionLocal() as session:
        run = (
            await session.execute(select(AiAnalysisRun).where(AiAnalysisRun.id == run_id))
        ).scalar_one()
        run.status = draft.status
        run.model_slug = draft.model_slug
        run.decision = draft.decision
        run.report_fa = draft.report_fa
        run.structured = draft.structured
        run.validation = draft.validation
        run.telegram_sent = draft.telegram_sent
        run.error_code = draft.error_code
        run.error_detail = draft.error_detail
        run.completed_at = completed
        cfg = await get_or_create_settings(session)
        record_run_outcome(cfg, run.status, draft.error_detail)
        await session.commit()
        await session.refresh(run)
        return run


async def _execute_inference(*, run_id: int, trigger: str, model_override: str | None) -> _RunDraft:
    """Network I/O only — no SQLite write transaction held on the run row."""
    _ = run_id
    settings = get_effective_settings()
    symbol = settings.default_symbol
    draft = _RunDraft()

    async with SessionLocal() as session:
        token, cred, err = await get_access_token(session)
        await session.commit()

    if err or not token:
        draft.error_code = err or "not_connected"
        draft.error_detail = "ChatGPT plan not connected"
        draft.completed_at = datetime.now(timezone.utc)
        return draft

    model = model_override or (cred.preferred_model if cred else None)
    if not model:
        try:
            models = await list_models(token)
            model = models[0]["slug"] if models else None
        except Exception as exc:
            draft.error_code = "model_list_failed"
            draft.error_detail = str(exc)[:500]
            draft.completed_at = datetime.now(timezone.utc)
            return draft
    if not model:
        draft.error_code = "no_model"
        draft.error_detail = "No listable model for this ChatGPT account"
        draft.completed_at = datetime.now(timezone.utc)
        return draft

    draft.model_slug = model
    try:
        ctx, live = await build_market_context(symbol)
        user_payload = json.dumps(ctx, ensure_ascii=False)
        text = await stream_response(
            token,
            model=model,
            instructions=INSTRUCTIONS,
            user_input=user_payload,
        )
        structured = _extract_json(text)
        validation = _validate_levels(structured, live)
        draft.decision = str(structured.get("decision", "NO_TRADE")).upper()
        draft.report_fa = structured.get("report_fa") or text
        draft.structured = structured
        draft.validation = validation
        draft.status = "completed" if validation.get("ok") else "completed_with_warnings"
        if validation.get("reasons"):
            draft.error_code = "validation_warning"
            draft.error_detail = ",".join(validation["reasons"])

        notifier = TelegramNotifier()
        if notifier.enabled and draft.report_fa:
            msg = format_ai_telegram_message(
                decision=draft.decision,
                report_fa=draft.report_fa,
                validation=validation,
            )
            msg_id = await notifier.send_ai_report(msg)
            draft.telegram_sent = msg_id is not None
    except MarketContextError as exc:
        draft.status = "failed"
        draft.error_code = exc.code
        draft.error_detail = exc.detail
    except OpenAIResponsesError as exc:
        draft.status = "failed"
        draft.error_code = exc.code
        draft.error_detail = str(exc)
    except Exception as exc:
        draft.status = "failed"
        draft.error_code = "analysis_error"
        draft.error_detail = str(exc)[:500]
    finally:
        draft.completed_at = datetime.now(timezone.utc)
    return draft


async def run_ai_analysis(*, trigger: str = "scheduled", model_override: str | None = None) -> AiAnalysisRun:
    run_id = await _create_run_record(trigger=trigger)
    draft = await _execute_inference(run_id=run_id, trigger=trigger, model_override=model_override)
    return await _persist_run_draft(run_id, draft)
