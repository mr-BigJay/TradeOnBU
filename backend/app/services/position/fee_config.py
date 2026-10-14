from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Official Bitunix VIP1 futures fees (maker / taker).
# Source: https://www.bitunix.com/service/handling-fee
BITUNIX_VIP1_FEE_SOURCE_URL = "https://www.bitunix.com/service/handling-fee"
BITUNIX_VIP1_FEE_VERIFIED_ON = date(2026, 10, 14)

# Rates as decimal fractions (0.0200% => 0.0002)
BITUNIX_VIP1_MAKER_RATE = 0.0002
BITUNIX_VIP1_TAKER_RATE = 0.0005


@dataclass(frozen=True)
class FeeProfile:
    fee_mode: str
    maker_rate: float
    taker_rate: float
    source_url: str
    verified_on: date


def default_vip1_profile() -> FeeProfile:
    return FeeProfile(
        fee_mode="bitunix_vip1",
        maker_rate=BITUNIX_VIP1_MAKER_RATE,
        taker_rate=BITUNIX_VIP1_TAKER_RATE,
        source_url=BITUNIX_VIP1_FEE_SOURCE_URL,
        verified_on=BITUNIX_VIP1_FEE_VERIFIED_ON,
    )
