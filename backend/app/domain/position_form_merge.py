from __future__ import annotations

from typing import Any


def merge_open_form_defaults(
    form: dict[str, Any],
    settings: dict[str, Any],
    *,
    preserve_execution: bool = False,
) -> dict[str, Any]:
    """Merge dashboard defaults without clobbering AI-prefilled execution fields."""
    out = dict(form)
    if settings.get("default_margin_usdt") is not None:
        out["margin_usdt"] = str(settings["default_margin_usdt"])
    if settings.get("default_leverage") is not None:
        out["leverage"] = str(settings["default_leverage"])
    if not preserve_execution and settings.get("default_order_type") is not None:
        out["order_type"] = str(settings["default_order_type"])
    return out
