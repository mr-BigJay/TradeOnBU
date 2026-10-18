from __future__ import annotations

from app.models import PriceForecastSettings


def estimate_forecast_cost_usd(
    settings: PriceForecastSettings,
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
) -> float:
    uncached = max(0, int(input_tokens) - int(cached_input_tokens))
    cost = (
        uncached / 1_000_000 * float(settings.input_price_per_mtok)
        + cached_input_tokens / 1_000_000 * float(settings.cached_input_price_per_mtok)
        + output_tokens / 1_000_000 * float(settings.output_price_per_mtok)
        + reasoning_tokens / 1_000_000 * float(settings.reasoning_price_per_mtok)
    )
    return round(cost, 6)
