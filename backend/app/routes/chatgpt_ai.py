from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import AiAnalysisRun, AiAnalysisSettings, SiwcConnectSession
from app.routes.auth import require_dashboard_access
from app.services.ai_analysis_scheduler import get_or_create_settings, scheduling_allowed
from app.services.ai_analysis_executor import execute_ai_analysis_with_lock
from app.services.siwc.connect_flow import new_connect_secret
from app.services.siwc.credentials import disconnect, get_access_token, get_credential_row
from app.services.siwc.host_id import load_or_create_host_id
from app.services.siwc.oauth import (
    DEFAULT_REDIRECT,
    build_authorize_url,
    has_plan_scope,
    new_pkce,
    scopes_list,
)
from app.services.siwc.responses_client import list_models

router = APIRouter(prefix="/api/gui/chatgpt", tags=["chatgpt-ai"])


class SettingsBody(BaseModel):
    enabled: bool = False
    unattended_consent: bool = False
    interval_minutes: int = Field(default=60, ge=15, le=240)
    preferred_model: str | None = None


@router.get("/status")
async def status(session: AsyncSession = Depends(get_db), _=Depends(require_dashboard_access)):
    cred = await get_credential_row(session)
    cfg = await get_or_create_settings(session)
    connected = bool(cred and cred.client_id and cred.access_token_enc)
    plan_ok = bool(cred and has_plan_scope(cred.scopes_json or []))
    sched_ok, sched_reason = await scheduling_allowed(session)
    return {
        "connected": connected,
        "plan_usage_authorized": plan_ok,
        "email": cred.email if cred else None,
        "client_id_masked": (cred.client_id[:8] + "…") if cred and cred.client_id else None,
        "preferred_model": cred.preferred_model if cred else None,
        "host_id": cred.ext_agent_host_id if cred else load_or_create_host_id(),
        "hourly_enabled": cfg.enabled,
        "unattended_consent": cfg.unattended_consent,
        "scheduling_active": sched_ok,
        "scheduling_block_reason": sched_reason,
        "interval_minutes": cfg.interval_minutes,
        "last_run_at": cfg.last_run_at.isoformat() if cfg.last_run_at else None,
        "last_success_at": cfg.last_success_at.isoformat() if cfg.last_success_at else None,
        "next_run_at": cfg.next_run_at.isoformat() if cfg.next_run_at else None,
        "last_error": cfg.last_error,
        "oauth_flow": "siwc_open_source",
        "connect_complete_url": "/api/public/siwc/connect/complete",
        "paper_only": True,
    }


@router.post("/connect/begin")
async def connect_begin(session: AsyncSession = Depends(get_db), _=Depends(require_dashboard_access)):
    cred = await get_credential_row(session)
    pkce = new_pkce()
    host_id = load_or_create_host_id()
    redirect_uri = DEFAULT_REDIRECT
    client_id = cred.client_id if cred and cred.client_id else "dynamic_agent_client"
    id_hint = None
    if cred and cred.id_token_enc:
        from app.services.crypto_secrets import decrypt_value

        id_hint = decrypt_value(cred.id_token_enc)
    connect_token = secrets.token_urlsafe(32)
    connect_secret, secret_hash = new_connect_secret()
    row = SiwcConnectSession(
        connect_token=connect_token,
        state=pkce.state,
        nonce=pkce.nonce,
        code_verifier=pkce.code_verifier,
        redirect_uri=redirect_uri,
        issued_client_id=client_id if client_id != "dynamic_agent_client" else None,
        secret_hash=secret_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    session.add(row)
    await session.commit()
    url = build_authorize_url(
        ext_agent_host_id=host_id,
        redirect_uri=redirect_uri,
        pkce=pkce,
        client_id=client_id,
        id_token_hint=id_hint,
        login_hint=cred.email if cred else None,
    )
    return {
        "connect_token": connect_token,
        "connect_secret": connect_secret,
        "oauth_state": pkce.state,
        "authorize_url": url,
        "redirect_uri": redirect_uri,
        "expires_in": 900,
        "complete_via": "POST /api/public/siwc/connect/complete with X-Connect-Secret (no dashboard session)",
        "helper_hint": "Run scripts/siwc_oauth_helper.py on Windows/Linux with a browser.",
    }


@router.post("/disconnect")
async def disconnect_route(session: AsyncSession = Depends(get_db), _=Depends(require_dashboard_access)):
    await disconnect(session)
    cfg = await get_or_create_settings(session)
    cfg.enabled = False
    cfg.unattended_consent = False
    cfg.unattended_consent_at = None
    await session.commit()
    return {"ok": True}


@router.get("/models")
async def models(session: AsyncSession = Depends(get_db), _=Depends(require_dashboard_access)):
    token, _, err = await get_access_token(session)
    if err or not token:
        raise HTTPException(400, err or "not_connected")
    return {"models": await list_models(token)}


@router.put("/settings")
async def put_settings(body: SettingsBody, session: AsyncSession = Depends(get_db), _=Depends(require_dashboard_access)):
    cred = await get_credential_row(session)
    if body.enabled and not (cred and cred.access_token_enc):
        raise HTTPException(400, "Connect ChatGPT before enabling scheduling")
    if body.enabled and not body.unattended_consent:
        raise HTTPException(400, "Explicit unattended ChatGPT usage consent is required to enable scheduling")
    cfg = await get_or_create_settings(session)
    cfg.enabled = body.enabled
    cfg.interval_minutes = body.interval_minutes
    if body.unattended_consent and not cfg.unattended_consent:
        cfg.unattended_consent = True
        cfg.unattended_consent_at = datetime.now(timezone.utc)
    elif not body.unattended_consent:
        cfg.unattended_consent = False
        cfg.unattended_consent_at = None
        cfg.enabled = False
    if cred and body.preferred_model:
        cred.preferred_model = body.preferred_model
    await session.commit()
    return {"ok": True}


@router.post("/analyze-now")
async def analyze_now(_=Depends(require_dashboard_access)):
    outcome = await execute_ai_analysis_with_lock(trigger="manual")
    if outcome.status == "busy":
        raise HTTPException(409, "AI analysis already running")
    run = outcome.run
    assert run is not None
    if run.status == "failed":
        raise HTTPException(502, run.error_detail or run.error_code or "failed")
    return {"ok": True, "run_id": run.id, "status": run.status, "decision": run.decision}


@router.get("/history")
async def history(limit: int = 50, session: AsyncSession = Depends(get_db), _=Depends(require_dashboard_access)):
    rows = (
        await session.execute(select(AiAnalysisRun).order_by(desc(AiAnalysisRun.id)).limit(min(limit, 200)))
    ).scalars().all()
    return [
        {
            "id": r.id,
            "status": r.status,
            "trigger": r.trigger,
            "decision": r.decision,
            "model_slug": r.model_slug,
            "telegram_sent": r.telegram_sent,
            "is_ai": r.is_ai,
            "error_code": r.error_code,
            "created_at": r.created_at.isoformat(),
            "report_preview": (r.report_fa or "")[:280],
        }
        for r in rows
    ]


@router.get("/history/{run_id}")
async def history_detail(run_id: int, session: AsyncSession = Depends(get_db), _=Depends(require_dashboard_access)):
    run = (await session.execute(select(AiAnalysisRun).where(AiAnalysisRun.id == run_id))).scalar_one_or_none()
    if not run:
        raise HTTPException(404, "Not found")
    return {
        "id": run.id,
        "status": run.status,
        "decision": run.decision,
        "report_fa": run.report_fa,
        "structured": run.structured,
        "validation": run.validation,
        "model_slug": run.model_slug,
        "telegram_sent": run.telegram_sent,
        "is_ai": run.is_ai,
        "error_code": run.error_code,
        "error_detail": run.error_detail,
        "created_at": run.created_at.isoformat(),
    }
