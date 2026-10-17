"""Paid API Luna reasoning (independent from SIWC profiles)."""

LUNA_API_EFFORTS = frozenset({"high", "max"})
DEFAULT_LUNA_API_EFFORT = "high"


def effective_luna_api_effort(value: str | None) -> str:
    """Use High for missing/invalid stored settings."""
    return value if value in LUNA_API_EFFORTS else DEFAULT_LUNA_API_EFFORT
