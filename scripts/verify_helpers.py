"""JSON helpers for verify_deployment.sh (also unit-tested)."""

from __future__ import annotations

import json
from typing import Any


def parse_json(text: str) -> Any:
    return json.loads(text)


def check_health_payload(data: dict[str, Any]) -> None:
    if data.get("paper_only") is not True:
        raise ValueError("paper_only must be true")
    if data.get("live_trading") is not False:
        raise ValueError("live_trading must be false")


def check_health_json(text: str) -> None:
    check_health_payload(parse_json(text))


def check_chart_payload(data: dict[str, Any]) -> None:
    if "candles" not in data:
        raise ValueError("missing candles key")


def check_chart_json(text: str) -> None:
    check_chart_payload(parse_json(text))
