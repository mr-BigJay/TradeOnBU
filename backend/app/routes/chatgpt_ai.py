from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import AiAnalysisRun, AiAnalysisSettings, SiwcConnectSession
from app.routes.auth import require_user
from app.services.ai_ict_analysis import acquire_hourly_lock, run_ai_analysis
from app.services.siwc.credentials import disconnect, get_access_token, get_credential_row, save_credentials
from app.services.siwc.host_id import load_or_create_host_id
from app.services.siwc.oauth import (
    DEFAULT_REDIRECT,
    build_authorize_url,
    exchange_code,
    has_plan_scope,
    new_pkce,
    scopes_list,
    validate_id_token,
)
from app.services.siwc.responses_client import list_models

router = APIRouter(prefix="/api/gui/chatgpt", tags=["chatgpt-ai"])


class ConnectCompleteBody(BaseModel):
    connect_token: str
    code: str
    callback_client_id: str | None = None


class SettingsBody(BaseModel):
    enabled: bool = True
    interval_minutes: int = Field(default=60, ge=15, le=240)
    preferred_model: str | None = None


@router.get("/status")
async def status(session: AsyncSession = Depends(get_db), _=Depends(require_user)):
    cred = await get_credential_row(session)
    cfg = (await session.execute(select(AiAnalysisSettings).where(AiAnalysisSettings.id == 1))).scalar_one_or_none()
    connected = bool(cred and cred.client_id and cred.access_token_enc)
    plan_ok = bool(cred and has_plan_scope(cred.scopes_json or []))
    return {
        "connected": connected,
        "plan_usage_authorized": plan_ok,
        "email": cred.email if cred else None,
        "client_id_masked": (cred.client_id[:8] + "…") if cred and cred.client_id else None,
        "preferred_model": cred.preferred_model if cred else None,
        "host_id": cred.ext_agent_host_id if cred else load_or_create_host_id(),
        "hourly_enabled": cfg.enabled if cfg else True,
        "interval_minutes": cfg.interval_minutes if cfg else 60,
        "last_run_at": cfg.last_run_at.isoformat() if cfg and cfg.last_run_at else None,
        "last_success_at": cfg.last_success_at.isoformat() if cfg and cfg.last_success_at else None,
        "last_error": cfg.last_error if cfg else None,
        "oauth_flow": "siwc_open_source",
        "paper_only": True,
    }


@router.post("/connect/begin")
async def connect_begin(session: AsyncSession = Depends(get_db), _=Depends(require_user)):
    cred = await get_credential_row(session)
    pkce = new_pkce()
    host_id = load_or_create_host_id()
    redirect_uri = DEFAULT_REDIRECT
    client_id = cred.client_id if cred and cred.client_id else "dynamic_agent_client"
    id_hint = None
    if cred and cred.id_token_enc:
        from app.services.crypto_secrets import decrypt_value

        id_hint = decrypt_value(cred.id_token_enc)
    token = secrets.token_urlsafe(32)
    row = SiwcConnectSession(
        connect_token=token,
        state=pkce.state,
        nonce=pkce.nonce,
        code_verifier=pkce.code_verifier,
        redirect_uri=redirect_uri,
        issued_client_id=client_id if client_id != "dynamic_agent_client" else None,
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
        "connect_token": token,
        "authorize_url": url,
        "redirect_uri": redirect_uri,
        "expires_in": 900,
        "helper_hint": "Run scripts/siwc_oauth_helper.py on a machine with a browser (see README).",
    }


@router.post("/connect/complete")
async def connect_complete(body: ConnectCompleteBody, session: AsyncSession = Depends(get_db), _=Depends(require_user)):
    pending = (
        await session.execute(
            select(SiwcConnectSession).where(SiwcConnectSession.connect_token == body.connect_token)
        )
    ).scalar_one_or_none()
    if not pending or pending.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(400, "Connect session expired or invalid")
    client_id = body.callback_client_id or pending.issued_client_id or "dynamic_agent_client"
    if client_id == "dynamic_agent_client":
        raise HTTPException(400, "Missing issued client_id from OAuth callback")
    try:
        token_response = await exchange_code(
            code=body.code,
            redirect_uri=pending.redirect_uri,
            code_verifier=pending.code_verifier,
            client_id=client_id,
        )
    except Exception as exc:
        raise HTTPException(400, f"Token exchange failed: {exc}") from exc
    id_token = token_response.get("id_token")
    if not id_token:
        raise HTTPException(400, "Missing id_token")
    claims = await validate_id_token(id_token, client_id=client_id, nonce=pending.nonce)
    scopes = scopes_list(token_response.get("scope"))
    if not has_plan_scope(scopes):
        raise HTTPException(403, "ChatGPT plan usage scope not granted")
    host_id = load_or_create_host_id()
    await save_credentials(
        session,
        email=claims.get("email"),
        subject=claims.get("sub"),
        client_id=client_id,
        ext_agent_host_id=host_id,
        token_response=token_response,
        id_token=id_token,
    )
    await session.delete(pending)
    await session.commit()
    return {"ok": True, "email": claims.get("email")}


@router.post("/disconnect")
async def disconnect_route(session: AsyncSession = Depends(get_db), _=Depends(require_user)):
    await disconnect(session)
    await session.commit()
    return {"ok": True}


@router.get("/models")
async def models(session: AsyncSession = Depends(get_db), _=Depends(require_user)):
    token, _, err = await get_access_token(session)
    if err or not token:
        raise HTTPException(400, err or "not_connected")
    return {"models": await list_models(token)}


@router.put("/settings")
async def put_settings(body: SettingsBody, session: AsyncSession = Depends(get_db), _=Depends(require_user)):
    cfg = (await session.execute(select(AiAnalysisSettings).where(AiAnalysisSettings.id == 1))).scalar_one_or_none()
    if cfg is None:
        cfg = AiAnalysisSettings(id=1)
        session.add(cfg)
    cfg.enabled = body.enabled
    cfg.interval_minutes = body.interval_minutes
    cred = await get_credential_row(session)
    if cred and body.preferred_model:
        cred.preferred_model = body.preferred_model
    await session.commit()
    return {"ok": True}


@router.post("/analyze-now")
async def analyze_now(session: AsyncSession = Depends(get_db), _=Depends(require_user)):
    run = await run_ai_analysis(session, trigger="manual")
    await session.commit()
    await session.refresh(run)
    if run.status == "failed":
        raise HTTPException(502, run.error_detail or run.error_code or "failed")
    return {"ok": True, "run_id": run.id, "status": run.status, "decision": run.decision}


@router.get("/history")
async def history(limit: int = 50, session: AsyncSession = Depends(get_db), _=Depends(require_user)):
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
async def history_detail(run_id: int, session: AsyncSession = Depends(get_db), _=Depends(require_user)):
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
