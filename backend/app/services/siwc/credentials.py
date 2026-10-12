from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatGptCredential
from app.services.crypto_secrets import decrypt_value, encrypt_value
from app.services.siwc.oauth import access_expiry, has_plan_scope, refresh_tokens, scopes_list


async def get_credential_row(session: AsyncSession) -> ChatGptCredential | None:
    return (await session.execute(select(ChatGptCredential).where(ChatGptCredential.id == 1))).scalar_one_or_none()


async def save_credentials(
    session: AsyncSession,
    *,
    email: str | None,
    subject: str | None,
    client_id: str,
    ext_agent_host_id: str,
    token_response: dict,
    id_token: str,
) -> ChatGptCredential:
    scopes = scopes_list(token_response.get("scope"))
    row = await get_credential_row(session)
    if row is None:
        row = ChatGptCredential(id=1)
        session.add(row)
    row.email = email
    row.subject = subject
    row.client_id = client_id
    row.ext_agent_host_id = ext_agent_host_id
    row.scopes_json = scopes
    row.access_token_enc = encrypt_value(token_response["access_token"])
    if token_response.get("refresh_token"):
        row.refresh_token_enc = encrypt_value(token_response["refresh_token"])
    row.id_token_enc = encrypt_value(id_token)
    row.access_expires_at = access_expiry(token_response.get("expires_in"))
    row.connected_at = datetime.now(timezone.utc)
    await session.flush()
    return row


async def disconnect(session: AsyncSession) -> None:
    row = await get_credential_row(session)
    if row:
        await session.delete(row)


def _decrypt(row: ChatGptCredential, field: str) -> str:
    enc = getattr(row, field)
    if not enc:
        return ""
    return decrypt_value(enc)


async def get_access_token(session: AsyncSession) -> tuple[str | None, ChatGptCredential | None, str | None]:
    row = await get_credential_row(session)
    if not row or not row.access_token_enc or not row.client_id:
        return None, row, "not_connected"
    if not has_plan_scope(row.scopes_json or []):
        return None, row, "plan_scope_missing"
    now = datetime.now(timezone.utc)
    if row.access_expires_at and row.access_expires_at > now + __import__("datetime").timedelta(seconds=60):
        return _decrypt(row, "access_token_enc"), row, None
    refresh = _decrypt(row, "refresh_token_enc")
    if not refresh:
        return _decrypt(row, "access_token_enc"), row, None
    try:
        refreshed = await refresh_tokens(refresh_token=refresh, client_id=row.client_id)
    except Exception as exc:
        return None, row, f"refresh_failed:{exc}"
    row.access_token_enc = encrypt_value(refreshed["access_token"])
    if refreshed.get("refresh_token"):
        row.refresh_token_enc = encrypt_value(refreshed["refresh_token"])
    row.access_expires_at = access_expiry(refreshed.get("expires_in"))
    row.scopes_json = scopes_list(refreshed.get("scope")) or row.scopes_json
    await session.flush()
    return refreshed["access_token"], row, None
