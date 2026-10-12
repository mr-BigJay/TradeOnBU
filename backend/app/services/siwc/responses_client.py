from __future__ import annotations

import json
from typing import Any

import httpx

OPENAI_RESPONSES = "https://api.openai.com/v1/responses"


class OpenAIResponsesError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def list_models(access_token: str) -> list[dict[str, str]]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
    out = []
    for m in data.get("models", []):
        if m.get("visibility") == "list":
            out.append({"slug": m.get("slug"), "display_name": m.get("display_name") or m.get("slug")})
    return out


async def stream_response(
    access_token: str,
    *,
    model: str,
    instructions: str,
    user_input: str,
    timeout: float = 120.0,
) -> str:
    payload: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": [{"role": "user", "content": user_input}],
        "store": False,
        "stream": True,
    }
    text_parts: list[str] = []
    completed = False
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            OPENAI_RESPONSES,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=payload,
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise OpenAIResponsesError("http_error", body.decode("utf-8", errors="replace")[:500])
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                raw = line[6:].strip()
                if raw == "[DONE]":
                    break
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                et = event.get("type")
                if et == "response.output_text.delta":
                    text_parts.append(event.get("delta") or "")
                elif et == "response.incomplete":
                    raise OpenAIResponsesError("response_incomplete", "Response incomplete")
                elif et == "response.failed":
                    err = (event.get("response") or {}).get("error") or {}
                    raise OpenAIResponsesError(err.get("code") or "response_failed", err.get("message") or "failed")
                elif et == "response.completed":
                    completed = True
                    return "".join(text_parts)
                elif et == "error":
                    err = event.get("error") or {}
                    raise OpenAIResponsesError(err.get("code") or "stream_error", err.get("message") or "error")
    if completed:
        return "".join(text_parts)
    if text_parts:
        raise OpenAIResponsesError("stream_interrupted", "Stream ended before response.completed")
    raise OpenAIResponsesError("incomplete", "Stream ended without response.completed")
