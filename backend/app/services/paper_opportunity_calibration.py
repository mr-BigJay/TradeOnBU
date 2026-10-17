"""Separate Fill vs Win calibration from auto-paper history."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.paper_execution_policy import (
    MIN_CALIBRATION_CLOSED_SAMPLES,
    MIN_CALIBRATION_ORDER_SAMPLES,
    TARGET_FILL_RATE,
    TARGET_WIN_RATE,
)
from app.models import SimPosition
from app.services.paper_calibration_stats import walk_forward_binary_rate, wilson_interval


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


FillStatus = Literal["uncalibrated", "calibrated"]
WinStatus = Literal["uncalibrated", "insufficient_closed_samples", "calibrated"]


@dataclass
class FillCalibration:
    status: FillStatus
    bucket: str
    orders_sample: int
    filled_count: int
    fill_rate: float | None
    ci_low: float | None
    ci_high: float | None
    target: float
    walk_forward_oos_rate: float | None
    walk_forward_oos_n: int


@dataclass
class WinCalibration:
    status: WinStatus
    bucket: str
    filled_sample: int
    closed_sample: int
    win_count: int
    win_rate: float | None
    ci_low: float | None
    ci_high: float | None
    target: float
    walk_forward_oos_rate: float | None
    walk_forward_oos_n: int


def _match_bucket_positions(
    positions: list[SimPosition],
    *,
    direction: str,
    order_type: str,
    band: str,
) -> list[SimPosition]:
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
    return matched


def _is_filled(p: SimPosition) -> bool:
    return p.fill_price is not None or p.status in ("open", "closed", "tp_hit", "sl_hit", "cancelled")


def _is_closed_with_pnl(p: SimPosition) -> bool:
    return p.status in ("closed", "tp_hit", "sl_hit") and p.net_pnl_usdt is not None


async def fill_calibration_for_bucket(
    session: AsyncSession,
    *,
    direction: str,
    order_type: str,
    distance_bps: float | None,
) -> FillCalibration:
    band = distance_band_bps(distance_bps)
    bucket = f"{direction.upper()}:{order_type.lower()}:{band}"
    positions = list((await session.execute(select(SimPosition).limit(5000))).scalars())
    matched = _match_bucket_positions(positions, direction=direction, order_type=order_type, band=band)
    matched.sort(key=lambda p: p.created_at or p.id)

    filled_flags = [_is_filled(p) for p in matched]
    filled_count = sum(filled_flags)
    n = len(matched)
    rate = filled_count / n if n else None
    ci_low, ci_high = wilson_interval(filled_count, n)
    wf = walk_forward_binary_rate(filled_flags)

    status: FillStatus = "uncalibrated"
    if n >= MIN_CALIBRATION_ORDER_SAMPLES and rate is not None:
        oos_ok = wf.oos_rate is None or wf.oos_n < 5 or wf.oos_rate >= TARGET_FILL_RATE * 0.85
        if rate >= TARGET_FILL_RATE and (ci_low is None or ci_low >= 0.55) and oos_ok:
            status = "calibrated"

    return FillCalibration(
        status=status,
        bucket=bucket,
        orders_sample=n,
        filled_count=filled_count,
        fill_rate=rate,
        ci_low=ci_low,
        ci_high=ci_high,
        target=TARGET_FILL_RATE,
        walk_forward_oos_rate=wf.oos_rate,
        walk_forward_oos_n=wf.oos_n,
    )


async def win_calibration_for_bucket(
    session: AsyncSession,
    *,
    direction: str,
    order_type: str,
    distance_bps: float | None,
) -> WinCalibration:
    band = distance_band_bps(distance_bps)
    bucket = f"{direction.upper()}:{order_type.lower()}:{band}"
    positions = list((await session.execute(select(SimPosition).limit(5000))).scalars())
    matched = _match_bucket_positions(positions, direction=direction, order_type=order_type, band=band)
    matched.sort(key=lambda p: p.created_at or p.id)

    filled = [p for p in matched if _is_filled(p)]
    closed = [p for p in filled if _is_closed_with_pnl(p)]
    wins = [p for p in closed if (p.net_pnl_usdt or 0) > 0]
    win_flags = [(p.net_pnl_usdt or 0) > 0 for p in closed]

    n_closed = len(closed)
    rate = len(wins) / n_closed if n_closed else None
    ci_low, ci_high = wilson_interval(len(wins), n_closed)
    wf = walk_forward_binary_rate(win_flags)

    status: WinStatus = "uncalibrated"
    if len(filled) >= MIN_CALIBRATION_ORDER_SAMPLES and n_closed < MIN_CALIBRATION_CLOSED_SAMPLES:
        status = "insufficient_closed_samples"
    elif n_closed >= MIN_CALIBRATION_CLOSED_SAMPLES and rate is not None:
        oos_ok = wf.oos_rate is None or wf.oos_n < 5 or wf.oos_rate >= TARGET_WIN_RATE * 0.85
        if rate >= TARGET_WIN_RATE and (ci_low is None or ci_low >= 0.45) and oos_ok:
            status = "calibrated"

    return WinCalibration(
        status=status,
        bucket=bucket,
        filled_sample=len(filled),
        closed_sample=n_closed,
        win_count=len(wins),
        win_rate=rate,
        ci_low=ci_low,
        ci_high=ci_high,
        target=TARGET_WIN_RATE,
        walk_forward_oos_rate=wf.oos_rate,
        walk_forward_oos_n=wf.oos_n,
    )
