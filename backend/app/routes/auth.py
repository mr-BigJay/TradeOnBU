from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import InstallationMeta, User, UserSession
from app.services.session_auth import (
    CSRF_HEADER,
    SESSION_COOKIE,
    authenticate,
    create_session,
    get_session_user,
    login_rate_limit_ok,
    record_login_failure,
    revoke_other_sessions,
    revoke_session,
)
from app.services.passwords import hash_password, password_strength_ok, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=1)


class PasswordChangeBody(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10)
    csrf_token: str | None = None


class UsernameChangeBody(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    current_password: str


class OnboardingBody(BaseModel):
    steps: dict[str, bool]


def _cookie_secure() -> bool:
    import os

    return os.getenv("SESSION_COOKIE_SECURE", "0") == "1"


async def require_user(request: Request, session: AsyncSession = Depends(get_db)) -> tuple[User, UserSession]:
    raw = request.cookies.get(SESSION_COOKIE)
    pair = await get_session_user(session, raw)
    if not pair:
        raise HTTPException(401, "Not authenticated")
    return pair


async def require_dashboard_access(
    request: Request, session: AsyncSession = Depends(get_db)
) -> tuple[User, UserSession] | None:
    """Session login in production; API key gate handled by middleware when session auth is off."""
    import os

    if os.getenv("SESSION_AUTH_ENABLED", "0") != "1":
        return None
    return await require_user(request, session)


@router.get("/csrf")
async def csrf_token(request: Request, session: AsyncSession = Depends(get_db)):
    raw = request.cookies.get(SESSION_COOKIE)
    pair = await get_session_user(session, raw)
    if not pair:
        return {"csrf_token": None, "authenticated": False}
    _, sess = pair
    return {"csrf_token": sess.csrf_token, "authenticated": True}


@router.get("/me")
async def me(request: Request, session: AsyncSession = Depends(get_db)):
    raw = request.cookies.get(SESSION_COOKIE)
    pair = await get_session_user(session, raw)
    if not pair:
        return {"authenticated": False}
    user, sess = pair
    meta = (await session.execute(select(InstallationMeta).where(InstallationMeta.id == 1))).scalar_one_or_none()
    return {
        "authenticated": True,
        "username": user.username,
        "is_admin": user.is_admin,
        "onboarding": user.onboarding or {},
        "csrf_token": sess.csrf_token,
        "domain": meta.domain if meta else None,
        "session_expires_at": sess.expires_at.isoformat(),
    }


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response, session: AsyncSession = Depends(get_db)):
    client = request.client.host if request.client else "unknown"
    if not login_rate_limit_ok(client):
        raise HTTPException(429, "Too many login attempts")
    meta = (await session.execute(select(InstallationMeta).where(InstallationMeta.id == 1))).scalar_one_or_none()
    if meta and not meta.installed:
        raise HTTPException(503, "Installation not complete")
    user = await authenticate(session, body.username, body.password)
    if not user:
        record_login_failure(client)
        raise HTTPException(401, "Invalid username or password")
    raw, csrf, _ = await create_session(
        session,
        user,
        ip=client,
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    response.set_cookie(
        SESSION_COOKIE,
        raw,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=12 * 3600,
        path="/",
    )
    return {"ok": True, "username": user.username, "csrf_token": csrf}


@router.post("/logout")
async def logout(request: Request, response: Response, session: AsyncSession = Depends(get_db)):
    raw = request.cookies.get(SESSION_COOKIE)
    await revoke_session(session, raw)
    await session.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/password")
async def change_password(
    body: PasswordChangeBody,
    request: Request,
    session: AsyncSession = Depends(get_db),
    user_pair: tuple[User, UserSession] = Depends(require_user),
):
    user, sess = user_pair
    if body.csrf_token and body.csrf_token != sess.csrf_token:
        raise HTTPException(403, "Invalid CSRF token")
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "Current password incorrect")
    ok, msg = password_strength_ok(body.new_password)
    if not ok:
        raise HTTPException(400, msg)
    user.password_hash = hash_password(body.new_password)
    await session.commit()
    return {"ok": True}


@router.post("/username")
async def change_username(
    body: UsernameChangeBody,
    session: AsyncSession = Depends(get_db),
    user_pair: tuple[User, UserSession] = Depends(require_user),
):
    user, _ = user_pair
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "Current password incorrect")
    user.username = body.username.strip()
    await session.commit()
    return {"ok": True, "username": user.username}


@router.get("/sessions")
async def list_sessions(
    session: AsyncSession = Depends(get_db),
    user_pair: tuple[User, UserSession] = Depends(require_user),
):
    user, current = user_pair
    rows = (await session.execute(select(UserSession).where(UserSession.user_id == user.id))).scalars().all()
    return [
        {
            "id": r.id,
            "ip_address": r.ip_address,
            "user_agent": r.user_agent,
            "created_at": r.created_at.isoformat(),
            "expires_at": r.expires_at.isoformat(),
            "current": r.id == current.id,
        }
        for r in rows
    ]


@router.post("/sessions/revoke-others")
async def revoke_others(
    request: Request,
    session: AsyncSession = Depends(get_db),
    user_pair: tuple[User, UserSession] = Depends(require_user),
):
    user, current = user_pair
    count = await revoke_other_sessions(session, user.id, current.token_hash)
    await session.commit()
    return {"ok": True, "revoked": count}


@router.put("/onboarding")
async def update_onboarding(
    body: OnboardingBody,
    session: AsyncSession = Depends(get_db),
    user_pair: tuple[User, UserSession] = Depends(require_user),
):
    user, _ = user_pair
    user.onboarding = {**(user.onboarding or {}), **body.steps}
    await session.commit()
    return {"ok": True, "onboarding": user.onboarding}
