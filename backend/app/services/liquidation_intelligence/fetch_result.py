from __future__ import annotations

from dataclasses import dataclass

from app.models import LiquidationEvent


@dataclass(frozen=True)
class FetchEventsResult:
    events: list[LiquidationEvent]
    truncated: bool
    row_limit: int
    rows_returned: int
    since_ms: int
    earliest_returned_ms: int | None
    matching_row_count: int | None = None

    def compact_warning(self) -> str | None:
        if not self.truncated:
            return None
        extra = ""
        if self.matching_row_count is not None:
            extra = f" ({self.matching_row_count} rows matched; cap {self.row_limit})"
        return f"Forecast query truncated{extra}; window metrics may omit older events"
