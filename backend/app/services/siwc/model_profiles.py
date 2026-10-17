from __future__ import annotations

DEFAULT_MODEL = "gpt-6.1-sol"
REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh", "max"})

MODEL_PROFILES: dict[str, dict[str, str]] = {
    "gpt-6.1-sol": {
        "model": "gpt-6.1-sol",
        "label": "GPT-6.1 Sol · Max",
        "reasoning_effort": "max",
        "description": "مدل فعلی با سطح استدلال حداکثری.",
    },
    "gpt-6-luna": {
        "model": "gpt-6-luna",
        "label": "GPT-6 Luna · High",
        "reasoning_effort": "high",
        "description": "مدل اقتصادی‌تر در API رسمی؛ دسترسی و سهمیه SIWC به حساب شما بستگی دارد.",
    },
}


def get_model_profile(model: str | None) -> dict[str, str] | None:
    if not isinstance(model, str):
        return None
    return MODEL_PROFILES.get(model.strip())


def list_model_profiles() -> list[dict[str, str]]:
    return [dict(profile) for profile in MODEL_PROFILES.values()]


def resolve_model_selection(
    preferred_model: str | None,
    configured_model: str,
    configured_effort: str,
) -> tuple[str, str]:
    """Resolve the stored UI choice or the existing environment defaults."""
    preferred = get_model_profile(preferred_model)
    if preferred is not None:
        return preferred["model"], preferred["reasoning_effort"]

    configured = get_model_profile(configured_model)
    if configured is None:
        configured = MODEL_PROFILES[DEFAULT_MODEL]
    effort = str(configured_effort or configured["reasoning_effort"]).lower()
    if effort not in REASONING_EFFORTS:
        effort = configured["reasoning_effort"]
    return configured["model"], effort
