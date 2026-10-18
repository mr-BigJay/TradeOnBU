from __future__ import annotations

from app.models import PriceForecastSettings


def estimate_forecast_cost_usd(
    settings: PriceForecastSettings,
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int = 0,
) -> float:
    """
    GPT-6 Luna Standard — reasoning_tokens are included in output_tokens billing only.
    """
    _ = reasoning_tokens
    cached = max(0, min(int(cached_input_tokens), int(input_tokens)))
    uncached = max(0, int(input_tokens) - cached)
    cost = (
        uncached / 1_000_000 * float(settings.input_price_per_mtok)
        + cached / 1_000_000 * float(settings.cached_input_price_per_mtok)
        + int(output_tokens) / 1_000_000 * float(settings.output_price_per_mtok)
    )
    return round(cost, 6)
