from __future__ import annotations

import os
import time
from collections import defaultdict

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.db import SessionLocal
from app.services.session_auth import CSRF_HEADER, SESSION_COOKIE, get_session_user


class SessionSecurityMiddleware(BaseHTTPMiddleware):
    PUBLIC_API = {
        "/api/health",
        "/api/auth/login",
        "/api/auth/csrf",
        "/api/auth/me",
        "/api/public/siwc/connect/complete",
    }
    CSRF_SAFE = {"GET", "HEAD", "OPTIONS"}
    CSRF_EXEMPT = {"/api/auth/login", "/api/auth/logout"}
    ADMIN_PREFIXES = (
        "/api/gui/daily-outlook/settings",
        "/api/gui/daily-outlook/generate",
        "/api/settings",
        "/api/gui/settings",
        "/api/worker",
        "/api/signals/import",
        "/api/signals/telegram-approval",
    )

    def __init__(self, app):
        super().__init__(app)
        self.settings = get_settings()
        self._hits: dict[str, list[float]] = defaultdict(list)
    def _session_mode(self) -> bool:
        return os.getenv("SESSION_AUTH_ENABLED", "0") == "1"

    def _rate_limit_ok(self, client: str) -> bool:
        now = time.time()
        window = self._hits[client]
        window[:] = [t for t in window if now - t < 60]
        if len(window) >= self.settings.rate_limit_per_minute:
            return False
        window.append(now)
        return True

    def _is_public(self, path: str) -> bool:
        if path in self.PUBLIC_API:
            return True
        if path.startswith("/api/telegram/webhook/"):
            return True
        if path == "/" or path.startswith("/assets/"):
            return True
        return False

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        client = request.client.host if request.client else "unknown"

        if path.startswith("/api") and not self._is_public(path):
            if not self._rate_limit_ok(client):
                return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)

            api_key = request.headers.get("X-API-Key", "")
            key_ok = api_key in (self.settings.dashboard_api_key, self.settings.admin_api_key) and bool(api_key)

            session_ok = False
            csrf_ok = True
            is_admin_session = False
            if self._session_mode():
                async with SessionLocal() as session:
                    pair = await get_session_user(session, request.cookies.get(SESSION_COOKIE))
                    if pair:
                        user, sess = pair
                        session_ok = True
                        is_admin_session = user.is_admin
                        if request.method not in self.CSRF_SAFE and path not in self.CSRF_EXEMPT:
                            hdr = request.headers.get(CSRF_HEADER, "")
                            csrf_ok = hdr == sess.csrf_token

            is_admin_path = any(path.startswith(p) for p in self.ADMIN_PREFIXES)

            if self._session_mode() and session_ok:
                if not csrf_ok:
                    return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
                if is_admin_path and not is_admin_session:
                    return JSONResponse({"detail": "Forbidden"}, status_code=403)
            elif self._session_mode():
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            else:
                if is_admin_path:
                    if api_key != self.settings.admin_api_key:
                        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
                elif api_key not in (self.settings.dashboard_api_key, self.settings.admin_api_key) or not api_key:
                    return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        return await call_next(request)

