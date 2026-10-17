from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import get_effective_settings
from app.db import SessionLocal
from app.models import AiAnalysisRun, AiProposedSetup
from app.services.ai_analysis_scheduler import get_or_create_settings, record_run_outcome
from app.domain.candles import Candle
from app.services.ai_market_payload import MarketContextError, build_raw_market_payload
from app.services.ai_setup_memory import build_ai_memory_context, create_setup_from_structured
from app.services.ai_telegram_format import format_ai_chart_caption, split_ai_telegram_report_messages
from app.services.chart_media import chart_artifact_meta, chart_path_for_run
from app.services.chart_renderer import ChartRenderError, render_ict_chart_png
from app.services.execution_model import ExecutionModel
from app.services.siwc.credentials import get_access_token
from app.services.siwc.model_profiles import get_model_profile, resolve_model_selection
from app.services.siwc.responses_client import AnalysisResponse, OpenAIResponsesError, stream_analysis_response
from app.services.ai_trader_instructions import ICT_TRADER_INSTRUCTIONS
from app.services.ai_trader_report import finalize_user_report, prepare_structured_for_user_report
from app.services.telegram_notifier import TelegramNotifier

INSTRUCTIONS = ICT_TRADER_INSTRUCTIONS


@dataclass
class _RunDraft:
    status: str = "failed"
    model_slug: str | None = None
    decision: str | None = None
    report_fa: str | None = None
    structured: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    telegram_sent: bool = False
    telegram_photo_sent: bool = False
    error_code: str | None = None
    error_detail: str | None = None
    completed_at: datetime | None = None


def _extract_json(text: str) -> dict[str, Any]:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    raw = match.group(1) if match else text.strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    return json.loads(raw)


def _paper_safety_notes(data: dict[str, Any], live: float) -> dict[str, Any]:
    """Objective checks only — never override AI decision."""
    decision = str(data.get("decision", "NO_TRADE")).upper()
    out: dict[str, Any] = {"ok": True, "warnings": [], "paper_action": "none", "ai_decision": decision}
    if decision == "NO_TRADE":
        return out
    if decision not in ("LONG", "SHORT"):
        out["warnings"].append("non_standard_decision_label")
        return out
    if live <= 0:
        out["warnings"].append("missing_live_price")
        return out
    try:
        entry = float(data["entry_price"])
        sl = float(data["stop_loss"])
        tp = float(data["take_profit"])
    except (KeyError, TypeError, ValueError):
        out["warnings"].append("missing_levels")
        return out
    drift = abs(entry - live) / live * 100
    if drift > 2.5:
        out["warnings"].append(f"entry_far_from_live:{drift:.2f}%")
    sl_dist_pct = abs(entry - sl) / entry * 100 if entry > 0 else 0.0
    tp_dist_pct = abs(tp - entry) / entry * 100 if entry > 0 else 0.0
    out["sl_distance_pct"] = round(sl_dist_pct, 4)
    out["tp1_distance_pct"] = round(tp_dist_pct, 4)
    if sl_dist_pct < 0.30:
        if data.get("sl_tight_structural"):
            out["warnings"].append(f"sl_tight_structural:{sl_dist_pct:.3f}%")
        else:
            out["warnings"].append(f"sl_below_guideline_review_noise:{sl_dist_pct:.3f}%")
    elif sl_dist_pct < 0.50:
        out["warnings"].append(f"sl_below_preferred_intraday:{sl_dist_pct:.3f}%")
    if tp_dist_pct < 0.80 and not data.get("tp_liquidity_target"):
        out["warnings"].append(f"tp1_move_small_no_liquidity_target:{tp_dist_pct:.3f}%")
    elif tp_dist_pct < 0.80:
        out["warnings"].append(f"tp1_move_below_guideline:{tp_dist_pct:.3f}%")
    model = ExecutionModel()
    rr = model.calc_rr(decision, entry, sl, tp, "LIMIT", min_net_rr=get_effective_settings().min_net_rr)
    out["net_rr"] = rr.net_rr
    out["acceptable_rr"] = rr.acceptable
    if not rr.acceptable:
        out["warnings"].append("net_rr_below_threshold")
    out["paper_action"] = "ai_proposal_only"
    return out


def _candles_from_payload(market: dict[str, Any], interval: str = "15m") -> list[Candle]:
    tf = (market.get("timeframes") or {}).get(interval) or {}
    rows = tf.get("candles") or []
    out: list[Candle] = []
    for row in rows:
        try:
            out.append(
                Candle(
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    time_ms=int(row["time_ms"]),
                    volume=float(row["volume"]) if row.get("volume") is not None else None,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _levels_for_chart(structured: dict[str, Any]) -> dict[str, float] | None:
    decision = str(structured.get("decision", "NO_TRADE")).upper()
    if decision not in ("LONG", "SHORT"):
        return None
    try:
        return {
            "entry": float(structured["entry_price"]),
            "stop_loss": float(structured["stop_loss"]),
            "take_profit": float(structured["take_profit"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _merge_artifacts(structured: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    merged = dict(structured)
    base = dict(merged.get("_artifacts") or {})
    for key, value in artifacts.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            nested = dict(base[key])
            nested.update(value)
            base[key] = nested
        else:
            base[key] = value
    merged["_artifacts"] = base
    return merged


def _chart_overlays_from_ai(structured: dict[str, Any]) -> dict[str, Any]:
    raw = structured.get("chart_overlays")
    if isinstance(raw, dict):
        return raw
    return {}


async def _generate_chart_for_run(
    *,
    run_id: int,
    symbol: str,
    market_payload: dict[str, Any],
    structured: dict[str, Any],
    live: float,
    decision: str,
    analyzed_at: datetime,
) -> tuple[Path | None, dict[str, Any]]:
    try:
        chart_path = chart_path_for_run(run_id)
        candles = _candles_from_payload(market_payload, "15m")
        overlays = _chart_overlays_from_ai(structured)
        trade_levels = _levels_for_chart(structured)
        try:
            await asyncio.to_thread(
                render_ict_chart_png,
                output_path=chart_path,
                symbol=symbol,
                interval="15m",
                candles=candles,
                overlays=overlays,
                live_price=live,
                decision=decision,
                trade_levels=trade_levels,
                analyzed_at=analyzed_at,
            )
        except ChartRenderError as exc:
            return None, _merge_artifacts(structured, {"chart": chart_artifact_meta(run_id, error=str(exc))})
        except Exception as exc:
            return None, _merge_artifacts(
                structured,
                {"chart": chart_artifact_meta(run_id, error=f"render_failed:{exc}")},
            )

        meta = chart_artifact_meta(run_id)
        meta["generated_at"] = analyzed_at.isoformat()
        return chart_path, _merge_artifacts(structured, {"chart": meta})
    except Exception as exc:
        return None, _merge_artifacts(
            structured,
            {"chart": chart_artifact_meta(run_id, error=f"unexpected:{exc}")},
        )


async def _deliver_ai_telegram(
    *,
    draft: _RunDraft,
    symbol: str,
    live: float,
    chart_path: Path | None,
    analyzed_at: datetime,
) -> None:
    if not draft.report_fa:
        return

    try:
        notifier = TelegramNotifier()
    except Exception as exc:
        draft.structured = _merge_artifacts(
            draft.structured,
            {"telegram": {"notifier_error": str(exc)[:500]}},
        )
        return

    if not notifier.enabled:
        return

    trade_levels = _levels_for_chart(draft.structured)
    telegram_meta: dict[str, Any] = {}

    if chart_path is not None:
        try:
            if chart_path.is_file():
                caption = format_ai_chart_caption(
                    symbol=symbol,
                    interval="15m",
                    decision=draft.decision,
                    live_price=live,
                    trade_levels=trade_levels,
                    analyzed_at=analyzed_at,
                )
                photo_id = await notifier.send_photo_file(chart_path, caption)
                draft.telegram_photo_sent = photo_id is not None
                telegram_meta["photo_message_id"] = photo_id
                if photo_id is None:
                    telegram_meta["photo_error"] = "send_photo_failed"
        except Exception as exc:
            telegram_meta["photo_error"] = str(exc)[:500]

    try:
        parts = split_ai_telegram_report_messages(
            decision=draft.decision,
            report_fa=draft.report_fa,
            validation=draft.validation,
        )
        text_ids: list[int] = []
        for part in parts:
            text_id = await notifier.send_ai_report(part)
            if text_id is not None:
                text_ids.append(text_id)
        draft.telegram_sent = len(text_ids) > 0
        telegram_meta["text_message_ids"] = text_ids
        telegram_meta["text_message_id"] = text_ids[0] if text_ids else None
        telegram_meta["text_part_count"] = len(parts)
        if parts and not text_ids:
            telegram_meta["text_error"] = "send_message_failed"
    except Exception as exc:
        telegram_meta["text_error"] = str(exc)[:500]

    draft.structured = _merge_artifacts(draft.structured, {"telegram": telegram_meta})


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


async def _persist_run_draft(run_id: int, draft: _RunDraft, *, symbol: str) -> AiAnalysisRun:
    completed = draft.completed_at or datetime.now(timezone.utc)
    async with SessionLocal() as session:
        run = (
            await session.execute(select(AiAnalysisRun).where(AiAnalysisRun.id == run_id))
        ).scalar_one()
        run.status = draft.status
        run.model_slug = draft.model_slug
        run.decision = draft.decision
        structured = draft.structured or {}
        run.final_ai_decision = str(structured.get("final_ai_decision") or draft.decision or "NO_TRADE").upper()
        run.setup_mode = str(structured.get("setup_mode") or "flat").lower()
        run.report_fa = draft.report_fa
        run.structured = draft.structured
        run.validation = draft.validation
        run.telegram_sent = draft.telegram_sent
        run.error_code = draft.error_code
        run.error_detail = draft.error_detail
        run.completed_at = completed
        setup_row: AiProposedSetup | None = None
        if draft.status.startswith("completed") and draft.structured:
            setup_row, setup_meta = await create_setup_from_structured(
                session, run_id=run_id, symbol=symbol, structured=draft.structured
            )
            draft.structured = _merge_artifacts(draft.structured, {"setup_persistence": setup_meta})
            run.structured = draft.structured
        cfg = await get_or_create_settings(session)
        record_run_outcome(cfg, run.status, draft.error_detail, trigger=run.trigger or "scheduled")
        if setup_row is not None and draft.status.startswith("completed"):
            draft.structured = _merge_artifacts(
                draft.structured,
                {
                    "opportunity": {
                        "setup_id": setup_row.setup_id,
                        "lifecycle_state": setup_row.lifecycle_state,
                        "setup_mode": setup_row.setup_mode,
                        "note": "Paper execution deferred to opportunity monitor until READY + objective confirmations.",
                    }
                },
            )
            run.structured = draft.structured
        await session.commit()
        await session.refresh(run)
        return run


async def _execute_inference(*, run_id: int, trigger: str, model_override: str | None) -> _RunDraft:
    settings = get_effective_settings()
    symbol = settings.default_symbol
    draft = _RunDraft()
    async with SessionLocal() as session:
        token, cred, err = await get_access_token(session)
        preferred_model = cred.preferred_model if cred and get_model_profile(cred.preferred_model) else None
        await session.commit()

    if err or not token:
        draft.error_code = err or "not_connected"
        draft.error_detail = "ChatGPT plan not connected"
        draft.completed_at = datetime.now(timezone.utc)
        return draft

    requested_model = model_override or preferred_model or settings.ai_analysis_model
    profile = get_model_profile(requested_model)
    if profile is None:
        draft.error_code = "model_not_supported"
        draft.error_detail = f"Model {requested_model} is not an allowed ICT analysis model"
        draft.completed_at = datetime.now(timezone.utc)
        return draft
    model, reasoning_effort = resolve_model_selection(
        model_override or preferred_model,
        settings.ai_analysis_model,
        settings.ai_reasoning_effort,
    )

    draft.model_slug = model
    try:
        from app.services.market_data import MarketDataService

        market_svc = MarketDataService(settings)
        market_payload, live, candles_by_interval = await build_raw_market_payload(
            symbol, settings, market=market_svc
        )
        async with SessionLocal() as session:
            memory = await build_ai_memory_context(
                session,
                symbol=symbol,
                candles_by_interval=candles_by_interval,
                market=market_svc,
            )
            await session.commit()

        user_payload = json.dumps(
            {
                "market": market_payload,
                "memory": memory,
                "requested_inference": {
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "trigger": trigger,
                },
            },
            ensure_ascii=False,
        )
        analysis: AnalysisResponse = await stream_analysis_response(
            token,
            model=model,
            instructions=INSTRUCTIONS,
            user_input=user_payload,
            reasoning_effort=reasoning_effort,
            timeout=settings.ai_response_timeout_seconds,
        )
        structured = _extract_json(analysis.text)
        structured = prepare_structured_for_user_report(structured)
        structured["report_fa"] = finalize_user_report(structured, symbol=symbol, live=live)
        safety = _paper_safety_notes(structured, live)
        draft.decision = str(structured.get("decision", "NO_TRADE")).upper()
        draft.report_fa = structured["report_fa"]
        draft.structured = structured
        draft.validation = safety
        draft.status = "completed"
        if safety.get("warnings"):
            draft.status = "completed_with_warnings"
            draft.error_code = "safety_warning"
            draft.error_detail = ",".join(safety["warnings"])

        inference_meta = {
            "requested_model": analysis.requested_model,
            "requested_reasoning_effort": analysis.requested_reasoning_effort,
            "confirmed_reasoning_effort": analysis.confirmed_reasoning_effort,
            "response_model": analysis.response_model,
            "response_id": analysis.response_id,
            "response_metadata": analysis.response_metadata,
        }
        draft.structured = _merge_artifacts(draft.structured, {"inference": inference_meta})

        analyzed_at = datetime.now(timezone.utc)
        chart_path: Path | None = None
        try:
            chart_path, structured_with_art = await _generate_chart_for_run(
                run_id=run_id,
                symbol=symbol,
                market_payload=market_payload,
                structured=draft.structured,
                live=live,
                decision=draft.decision or "NO_TRADE",
                analyzed_at=analyzed_at,
            )
            draft.structured = structured_with_art
        except Exception as exc:
            draft.structured = _merge_artifacts(
                draft.structured,
                {"chart": chart_artifact_meta(run_id, error=f"unexpected:{exc}")},
            )

        try:
            await _deliver_ai_telegram(
                draft=draft,
                symbol=symbol,
                live=live,
                chart_path=chart_path,
                analyzed_at=analyzed_at,
            )
        except Exception as exc:
            draft.structured = _merge_artifacts(
                draft.structured,
                {"telegram": {"delivery_error": str(exc)[:500]}},
            )
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
    settings = get_effective_settings()
    run_id = await _create_run_record(trigger=trigger)
    draft = await _execute_inference(run_id=run_id, trigger=trigger, model_override=model_override)
    return await _persist_run_draft(run_id, draft, symbol=settings.default_symbol)


# Backward-compatible alias for tests/imports
async def build_market_context(symbol: str) -> tuple[dict[str, Any], float]:
    payload, live, _ = await build_raw_market_payload(symbol)
    return payload, live
