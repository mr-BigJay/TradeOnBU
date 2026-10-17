from __future__ import annotations

import httpx


class ApiModelAccessError(Exception):
    """Only safe error codes; no API key or raw provider body can be exposed."""


async def api_model_accessible(api_key: str, model: str) -> bool:
    if not api_key.strip():
        raise ApiModelAccessError("openai_api_key_missing")
    if model != "gpt-6-luna":
        return False
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                "https://api.openai.com/v1/models/gpt-6-luna",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as exc:
        raise ApiModelAccessError("openai_api_unreachable") from exc
    if response.status_code == 200:
        return True
    if response.status_code == 404:
        return False
    if response.status_code in (401, 403):
        raise ApiModelAccessError("openai_api_auth_failed")
    if response.status_code == 429:
        raise ApiModelAccessError("openai_api_rate_limited")
    raise ApiModelAccessError(f"openai_api_http_{response.status_code}")
