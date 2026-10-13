from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

OPENAI_RESPONSES = "https://api.openai.com/v1/responses"


class OpenAIResponsesError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class AnalysisResponse:
    text: str
    requested_model: str
    requested_reasoning_effort: str
    response_model: str | None = None
    confirmed_reasoning_effort: str | None = None
    response_id: str | None = None
    response_metadata: dict[str, Any] = field(default_factory=dict)


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


def _extract_response_meta(response_obj: dict[str, Any]) -> tuple[str | None, str | None, dict[str, Any]]:
    if not response_obj:
        return None, None, {}
    model = response_obj.get("model")
    resp_id = response_obj.get("id")
    meta = {
        "status": response_obj.get("status"),
        "reasoning": response_obj.get("reasoning"),
        "usage": response_obj.get("usage"),
    }
    return model, resp_id, meta


def _ensure_no_reasoning_downgrade(requested: str, response_obj: dict[str, Any]) -> None:
    """Fail closed if API signals a lower reasoning effort than requested."""
    if requested.lower() != "max":
        return
    reasoning = response_obj.get("reasoning") if response_obj else None
    if isinstance(reasoning, dict):
        effort = str(reasoning.get("effort") or "").lower()
        if effort and effort != "max":
            raise OpenAIResponsesError(
                "reasoning_downgraded",
                f"Requested reasoning effort max but response reported effort={effort}",
            )


async def stream_analysis_response(
    access_token: str,
    *,
    model: str,
    instructions: str,
    user_input: str,
    reasoning_effort: str = "max",
    timeout: float = 600.0,
) -> AnalysisResponse:
    effort = (reasoning_effort or "max").lower()
    if effort != "max":
        raise OpenAIResponsesError("invalid_reasoning_effort", f"Only reasoning effort max is allowed, got {effort}")

    payload: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": [{"role": "user", "content": user_input}],
        "reasoning": {"effort": "max"},
        "store": False,
        "stream": True,
    }
    text_parts: list[str] = []
    response_obj: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            OPENAI_RESPONSES,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=payload,
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                detail = body.decode("utf-8", errors="replace")[:800]
                code = "http_error"
                lower = detail.lower()
                if "reasoning" in lower and ("unsupported" in lower or "invalid" in lower or "not available" in lower):
                    code = "reasoning_max_unavailable"
                raise OpenAIResponsesError(code, detail)
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
                    code = err.get("code") or "response_failed"
                    msg = err.get("message") or "failed"
                    if "reasoning" in str(msg).lower():
                        code = "reasoning_max_unavailable"
                    raise OpenAIResponsesError(code, msg)
                elif et == "response.completed":
                    response_obj = event.get("response") or {}
                    _ensure_no_reasoning_downgrade("max", response_obj)
                    resp_model, resp_id, meta = _extract_response_meta(response_obj)
                    confirmed_effort: str | None = None
                    reasoning = response_obj.get("reasoning") if response_obj else None
                    if isinstance(reasoning, dict):
                        confirmed_effort = str(reasoning.get("effort") or "") or None
                    if resp_model and resp_model != model:
                        raise OpenAIResponsesError(
                            "model_downgraded",
                            f"Requested model {model} but response model was {resp_model}",
                        )
                    return AnalysisResponse(
                        text="".join(text_parts),
                        requested_model=model,
                        requested_reasoning_effort="max",
                        response_model=resp_model,
                        confirmed_reasoning_effort=confirmed_effort,
                        response_id=resp_id,
                        response_metadata=meta,
                    )
                elif et == "error":
                    err = event.get("error") or {}
                    code = err.get("code") or "stream_error"
                    msg = err.get("message") or "error"
                    if "reasoning" in str(msg).lower():
                        code = "reasoning_max_unavailable"
                    raise OpenAIResponsesError(code, msg)

    if text_parts:
        raise OpenAIResponsesError("stream_interrupted", "Stream ended before response.completed")
    raise OpenAIResponsesError("incomplete", "Stream ended without response.completed")


async def stream_response(
    access_token: str,
    *,
    model: str,
    instructions: str,
    user_input: str,
    timeout: float = 120.0,
) -> str:
    """Legacy helper — prefer stream_analysis_response for AI ICT runs."""
    result = await stream_analysis_response(
        access_token,
        model=model,
        instructions=instructions,
        user_input=user_input,
        reasoning_effort="max",
        timeout=timeout,
    )
    return result.text
