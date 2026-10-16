"""Historical fill/win rates for similar auto-paper setups (no fabricated probabilities)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.paper_execution_policy import MIN_CALIBRATION_SAMPLES
from app.models import AiProposedSetup, SimPosition


def distance_band_bps(bps: float | None) -> str:
    if bps is None:
        return "unknown"
    if bps <= 20:
        return "0-20"
    if bps <= 65:
        return "21-65"
    if bps <= 120:
        return "66-120"
    return "120+"


@dataclass
class CalibratedRates:
    status: Literal["calibrated", "uncalibrated"]
    sample_size: int
    fill_rate: float | None
    win_rate: float | None
    bucket: str


async def calibrated_rates_for_bucket(
    session: AsyncSession,
    *,
    direction: str,
    order_type: str,
    distance_bps: float | None,
) -> CalibratedRates:
    band = distance_band_bps(distance_bps)
    bucket = f"{direction.upper()}:{order_type.lower()}:{band}"
    positions = list((await session.execute(select(SimPosition).limit(2000))).scalars())
    matched: list[SimPosition] = []
    for p in positions:
        extra = p.extra or {}
        if not extra.get("auto_paper"):
            continue
        if (p.direction or "").upper() != direction.upper():
            continue
        if (p.order_type or "").lower() != order_type.lower():
            continue
        pb = extra.get("distance_bps_at_order")
        if distance_band_bps(float(pb) if pb is not None else None) != band:
            continue
        matched.append(p)

    if len(matched) < MIN_CALIBRATION_SAMPLES:
        return CalibratedRates(
            status="uncalibrated",
            sample_size=len(matched),
            fill_rate=None,
            win_rate=None,
            bucket=bucket,
        )

    filled = [p for p in matched if p.fill_price is not None or p.status in ("open", "closed", "tp_hit", "sl_hit")]
    fill_rate = len(filled) / len(matched) if matched else None
    closed = [p for p in filled if p.status in ("closed", "tp_hit", "sl_hit") and p.net_pnl_usdt is not None]
    wins = [p for p in closed if (p.net_pnl_usdt or 0) > 0]
    win_rate = (len(wins) / len(closed)) if closed else None

    return CalibratedRates(
        status="calibrated",
        sample_size=len(matched),
        fill_rate=fill_rate,
        win_rate=win_rate,
        bucket=bucket,
    )
