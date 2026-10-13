from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Candle:
    open: float
    high: float
    low: float
    close: float
    time_ms: int
    volume: float | None = None

    @property
    def open_time(self) -> datetime:
        return datetime.utcfromtimestamp(self.time_ms / 1000)

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    @property
    def body_high(self) -> float:
        return max(self.open, self.close)

    @property
    def body_low(self) -> float:
        return min(self.open, self.close)
