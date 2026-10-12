from __future__ import annotations

import hashlib
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserSession
from app.services.passwords import hash_password, password_strength_ok, verify_password

SESSION_COOKIE = "tradeonbu_session"
CSRF_HEADER = "X-CSRF-Token"
SESSION_HOURS = 12
LOGIN_WINDOW_SEC = 900
MAX_LOGIN_ATTEMPTS = 8

_login_attempts: dict[str, list[float]] = defaultdict(list)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def login_rate_limit_ok(client_key: str) -> bool:
    now = time.time()
    arr = _login_attempts[client_key]
    arr[:] = [t for t in arr if now - t < LOGIN_WINDOW_SEC]
    return len(arr) < MAX_LOGIN_ATTEMPTS


def record_login_failure(client_key: str) -> None:
    _login_attempts[client_key].append(time.time())


async def create_admin_user(session: AsyncSession, username: str, password: str) -> User:
    ok, msg = password_strength_ok(password)
    if not ok:
        raise ValueError(msg)
    existing = (await session.execute(select(User))).scalars().first()
    if existing:
        raise ValueError("admin already exists")
    user = User(username=username.strip(), password_hash=hash_password(password), is_admin=True)
    session.add(user)
    await session.flush()
    return user


async def authenticate(session: AsyncSession, username: str, password: str) -> User | None:
    user = (await session.execute(select(User).where(User.username == username.strip()))).scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        return None
    return user


async def create_session(
    session: AsyncSession,
    user: User,
    ip: str | None,
    user_agent: str | None,
) -> tuple[str, str, UserSession]:
    raw = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    row = UserSession(
        user_id=user.id,
        token_hash=_hash_token(raw),
        csrf_token=csrf,
        ip_address=ip,
        user_agent=(user_agent or "")[:255],
        expires_at=datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS),
    )
    session.add(row)
    await session.flush()
    return raw, csrf, row


async def get_session_user(session: AsyncSession, raw_token: str | None) -> tuple[User, UserSession] | None:
    if not raw_token:
        return None
    h = _hash_token(raw_token)
    row = (await session.execute(select(UserSession).where(UserSession.token_hash == h))).scalar_one_or_none()
    if not row:
        return None
    if row.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        await session.execute(delete(UserSession).where(UserSession.id == row.id))
        return None
    user = (await session.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    if not user:
        return None
    return user, row


async def revoke_session(session: AsyncSession, raw_token: str | None) -> None:
    if not raw_token:
        return
    h = _hash_token(raw_token)
    await session.execute(delete(UserSession).where(UserSession.token_hash == h))


async def revoke_other_sessions(session: AsyncSession, user_id: int, keep_hash: str) -> int:
    result = await session.execute(
        delete(UserSession).where(UserSession.user_id == user_id, UserSession.token_hash != keep_hash)
    )
    return result.rowcount or 0
