from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import ChatGptCredential, SiwcConnectSession
from app.services.siwc.credentials import get_credential_row, save_credentials
from app.services.siwc.host_id import load_or_create_host_id
from app.services.siwc.oauth import exchange_code, has_plan_scope, scopes_list, validate_id_token


def hash_connect_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_connect_secret(secret: str, secret_hash: str) -> bool:
    return hmac.compare_digest(hash_connect_secret(secret), secret_hash)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class _PendingOAuth:
    code_verifier: str
    redirect_uri: str
    nonce: str
    client_id: str
    connect_token: str


async def _consume_pending_session(
    session: AsyncSession,
    *,
    connect_token: str,
    connect_secret: str,
    oauth_state: str,
    callback_client_id: str | None,
) -> _PendingOAuth:
    pending = (
        await session.execute(select(SiwcConnectSession).where(SiwcConnectSession.connect_token == connect_token))
    ).scalar_one_or_none()
    if not pending:
        raise HTTPException(400, "Connect session invalid")
    if pending.consumed_at is not None:
        raise HTTPException(400, "Connect session already used")
    if _ensure_utc(pending.expires_at) < datetime.now(timezone.utc):
        raise HTTPException(400, "Connect session expired")
    if not verify_connect_secret(connect_secret, pending.secret_hash):
        raise HTTPException(403, "Invalid connect secret")
    if not hmac.compare_digest(oauth_state, pending.state):
        raise HTTPException(400, "OAuth state mismatch")

    expected_client = pending.issued_client_id
    issued = callback_client_id or expected_client
    if expected_client and expected_client != "dynamic_agent_client":
        if not issued or issued != expected_client:
            raise HTTPException(400, "OAuth client_id mismatch")
    elif issued and issued == "dynamic_agent_client":
        raise HTTPException(400, "Missing issued client_id from OAuth callback")

    client_id = issued or expected_client
    if not client_id or client_id == "dynamic_agent_client":
        raise HTTPException(400, "Missing issued client_id")

    if expected_client and callback_client_id and callback_client_id != expected_client:
        raise HTTPException(400, "Callback client_id does not match pending session")

    pending.consumed_at = datetime.now(timezone.utc)
    snapshot = _PendingOAuth(
        code_verifier=pending.code_verifier,
        redirect_uri=pending.redirect_uri,
        nonce=pending.nonce,
        client_id=client_id,
        connect_token=connect_token,
    )
    await session.commit()
    return snapshot


async def consume_connect_session(
    *,
    connect_token: str,
    connect_secret: str,
    oauth_state: str,
    code: str,
    callback_client_id: str | None,
) -> dict:
    async with SessionLocal() as session:
        snapshot = await _consume_pending_session(
            session,
            connect_token=connect_token,
            connect_secret=connect_secret,
            oauth_state=oauth_state,
            callback_client_id=callback_client_id,
        )

    try:
        token_response = await exchange_code(
            code=code,
            redirect_uri=snapshot.redirect_uri,
            code_verifier=snapshot.code_verifier,
            client_id=snapshot.client_id,
        )
    except Exception as exc:
        raise HTTPException(400, f"Token exchange failed: {exc}") from exc

    id_token = token_response.get("id_token")
    if not id_token:
        raise HTTPException(400, "Missing id_token")

    claims = await validate_id_token(id_token, client_id=snapshot.client_id, nonce=snapshot.nonce)
    scopes = scopes_list(token_response.get("scope"))
    if not has_plan_scope(scopes):
        raise HTTPException(403, "ChatGPT plan usage scope not granted")

    async with SessionLocal() as save_session:
        existing: ChatGptCredential | None = await get_credential_row(save_session)
        if existing and existing.subject and claims.get("sub") != existing.subject:
            raise HTTPException(409, "ChatGPT account subject does not match existing connection")

        host_id = load_or_create_host_id()
        await save_credentials(
            save_session,
            email=claims.get("email"),
            subject=claims.get("sub"),
            client_id=snapshot.client_id,
            ext_agent_host_id=host_id,
            token_response=token_response,
            id_token=id_token,
        )
        row = (
            await save_session.execute(
                select(SiwcConnectSession).where(SiwcConnectSession.connect_token == snapshot.connect_token)
            )
        ).scalar_one_or_none()
        if row:
            await save_session.delete(row)
        await save_session.commit()

    return {"ok": True, "email": claims.get("email")}


def new_connect_secret() -> tuple[str, str]:
    secret = secrets.token_urlsafe(32)
    return secret, hash_connect_secret(secret)
