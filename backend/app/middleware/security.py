from __future__ import annotations

import time
from collections import defaultdict

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings


class SecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.settings = get_settings()
        self._hits: dict[str, list[float]] = defaultdict(list)

    def _rate_limit_ok(self, client: str) -> bool:
        now = time.time()
        window = self._hits[client]
        window[:] = [t for t in window if now - t < 60]
        if len(window) >= self.settings.rate_limit_per_minute:
            return False
        window.append(now)
        return True

    def _is_public(self, path: str) -> bool:
        if path == "/api/health":
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

            if not self.settings.dashboard_api_key and not self.settings.admin_api_key:
                return await call_next(request)

            api_key = request.headers.get("X-API-Key", "")
            is_admin_path = path.startswith("/api/settings") or path.startswith("/api/worker") or path.startswith(
                "/api/signals/import"
            )
            if is_admin_path:
                if self.settings.admin_api_key and api_key != self.settings.admin_api_key:
                    return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            elif self.settings.dashboard_api_key and api_key != self.settings.dashboard_api_key:
                if api_key != self.settings.admin_api_key:
                    return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        return await call_next(request)
