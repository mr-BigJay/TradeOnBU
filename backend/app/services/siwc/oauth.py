from __future__ import annotations

import base64
import hashlib
import secrets
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import jwt

OPENAI_AUTH = "https://auth.openai.com"
OPENAI_API = "https://api.openai.com/v1"
JWKS_URL = f"{OPENAI_AUTH}/api/accounts/oauth/jwks"
TOKEN_URL = f"{OPENAI_AUTH}/api/accounts/oauth/token"
AUTHORIZE_URL = f"{OPENAI_AUTH}/api/accounts/authorize"

SIWC_SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
REQUIRED_PLAN_SCOPE = "chatgpt.tokens.use.direct"
DEFAULT_REDIRECT = "http://127.0.0.1:1455/auth/callback"
AGENT_NAME = "TradeOnBU"


@dataclass
class PkceBundle:
    state: str
    nonce: str
    code_verifier: str
    code_challenge: str


def new_pkce() -> PkceBundle:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return PkceBundle(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        code_verifier=verifier,
        code_challenge=challenge,
    )


def build_authorize_url(
    *,
    ext_agent_host_id: str,
    redirect_uri: str,
    pkce: PkceBundle,
    client_id: str = "dynamic_agent_client",
    id_token_hint: str | None = None,
    login_hint: str | None = None,
) -> str:
    params = {
        "client_id": client_id,
        "agent_name_hint": AGENT_NAME,
        "ext_agent_host_id": ext_agent_host_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SIWC_SCOPES,
        "resource": OPENAI_API,
        "state": pkce.state,
        "nonce": pkce.nonce,
        "code_challenge_method": "S256",
        "code_challenge": pkce.code_challenge,
    }
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


async def exchange_code(
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    client_id: str,
) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
                "resource": OPENAI_API,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()


async def revoke_token(*, token: str, client_id: str, token_type_hint: str = "refresh_token") -> None:
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{OPENAI_AUTH}/api/accounts/oauth/revoke",
            data={
                "client_id": client_id,
                "token": token,
                "token_type_hint": token_type_hint,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400 and resp.status_code != 404:
            resp.raise_for_status()


async def refresh_tokens(*, refresh_token: str, client_id: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
                "resource": OPENAI_API,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()


_jwks_cache: dict | None = None


async def _jwks() -> dict:
    global _jwks_cache
    if _jwks_cache:
        return _jwks_cache
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(JWKS_URL)
        resp.raise_for_status()
        _jwks_cache = resp.json()
        return _jwks_cache


async def validate_id_token(id_token: str, *, client_id: str, nonce: str) -> dict:
    jwks = await _jwks()
    header = jwt.get_unverified_header(id_token)
    kid = header.get("kid")
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    if not key:
        raise ValueError("JWKS key not found for id_token")
    claims = jwt.decode(
        id_token,
        jwt.algorithms.RSAAlgorithm.from_jwk(key),
        algorithms=["RS256"],
        audience=client_id,
        options={"require": ["exp", "sub"], "verify_iss": False},
    )
    iss = claims.get("iss", "")
    if iss not in (OPENAI_AUTH, f"{OPENAI_AUTH}/"):
        raise ValueError("Unexpected id_token issuer")
    if claims.get("nonce") != nonce:
        raise ValueError("ID token nonce mismatch")
    return claims


def scopes_list(scope_field: str | list | None) -> list[str]:
    if scope_field is None:
        return []
    if isinstance(scope_field, list):
        return scope_field
    return [s for s in str(scope_field).split() if s]


def has_plan_scope(scopes: list[str]) -> bool:
    return REQUIRED_PLAN_SCOPE in scopes


def access_expiry(expires_in: int | None) -> datetime | None:
    if not expires_in:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
