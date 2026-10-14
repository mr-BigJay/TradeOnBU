from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SimPositionSettings
from app.services.position.fee_config import (
    BITUNIX_VIP1_FEE_SOURCE_URL,
    BITUNIX_VIP1_FEE_VERIFIED_ON,
    BITUNIX_VIP1_MAKER_RATE,
    BITUNIX_VIP1_TAKER_RATE,
    FeeProfile,
)


async def get_or_create_settings(session: AsyncSession) -> SimPositionSettings:
    result = await session.execute(select(SimPositionSettings).where(SimPositionSettings.id == 1))
    row = result.scalar_one_or_none()
    if row:
        return row
    row = SimPositionSettings(
        id=1,
        maker_fee_rate=BITUNIX_VIP1_MAKER_RATE,
        taker_fee_rate=BITUNIX_VIP1_TAKER_RATE,
        fee_source_url=BITUNIX_VIP1_FEE_SOURCE_URL,
        fee_verified_on=BITUNIX_VIP1_FEE_VERIFIED_ON.isoformat(),
    )
    session.add(row)
    await session.flush()
    return row


def fee_profile_from_settings(settings: SimPositionSettings) -> FeeProfile:
    verified = None
    if settings.fee_verified_on:
        from datetime import date

        try:
            verified = date.fromisoformat(settings.fee_verified_on)
        except ValueError:
            verified = BITUNIX_VIP1_FEE_VERIFIED_ON
    else:
        verified = BITUNIX_VIP1_FEE_VERIFIED_ON
    return FeeProfile(
        fee_mode=settings.fee_mode,
        maker_rate=settings.maker_fee_rate,
        taker_rate=settings.taker_fee_rate,
        source_url=settings.fee_source_url,
        verified_on=verified,
    )
