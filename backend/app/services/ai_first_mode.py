from __future__ import annotations

from app.config import get_effective_settings


def legacy_rule_scan_enabled() -> bool:
    """When AI-first is active, automatic rule-based ICT scanning and alerts are disabled."""
    from app.services.verified_trade_plan import enabled
    if enabled():
        return False
    settings = get_effective_settings()
    return not settings.ai_first_analyst_mode
