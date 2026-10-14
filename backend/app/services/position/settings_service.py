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


class SettingsValidationError(ValueError):
    pass


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


def validate_settings_patch(data: dict) -> dict:
    out = dict(data)
    if "maker_fee_rate" in out and out["maker_fee_rate"] is not None:
        if out["maker_fee_rate"] < 0:
            raise SettingsValidationError("کارمزد maker نمی‌تواند منفی باشد")
    if "taker_fee_rate" in out and out["taker_fee_rate"] is not None:
        if out["taker_fee_rate"] < 0:
            raise SettingsValidationError("کارمزد taker نمی‌تواند منفی باشد")
    if "default_leverage" in out and out["default_leverage"] is not None:
        lev = int(out["default_leverage"])
        if lev < 1 or lev > 125:
            raise SettingsValidationError("اهرم باید بین ۱ و ۱۲۵ باشد")
        out["default_leverage"] = lev
    if "slippage_bps" in out and out["slippage_bps"] is not None:
        if out["slippage_bps"] < 0:
            raise SettingsValidationError("اسلیپیج نمی‌تواند منفی باشد")
    if "default_order_type" in out and out["default_order_type"] is not None:
        ot = str(out["default_order_type"]).lower()
        if ot not in ("market", "limit"):
            raise SettingsValidationError("نوع سفارش نامعتبر")
        out["default_order_type"] = ot
    if "max_margin_per_position" in out and out["max_margin_per_position"] is not None:
        if out["max_margin_per_position"] <= 0:
            raise SettingsValidationError("حداکثر مارجین باید مثبت باشد")
    if "max_risk_pct_per_position" in out and out["max_risk_pct_per_position"] is not None:
        if out["max_risk_pct_per_position"] <= 0:
            raise SettingsValidationError("حداکثر ریسک باید مثبت باشد")
    return out


def validate_order_request_fields(
    *,
    direction: str,
    leverage: int,
    margin_usdt: float,
    order_type: str,
    entry_price: float | None,
    stop_loss: float,
    take_profit: float,
) -> None:
    if direction not in ("LONG", "SHORT"):
        raise SettingsValidationError("جهت نامعتبر")
    if leverage < 1 or leverage > 125:
        raise SettingsValidationError("اهرم خارج از محدوده")
    if margin_usdt <= 0:
        raise SettingsValidationError("مارجین باید مثبت باشد")
    ot = order_type.lower()
    if ot not in ("market", "limit"):
        raise SettingsValidationError("نوع سفارش نامعتبر")
    if stop_loss <= 0 or take_profit <= 0:
        raise SettingsValidationError("SL/TP باید مثبت باشند")
    if ot == "limit" and (entry_price is None or entry_price <= 0):
        raise SettingsValidationError("قیمت ورود نامعتبر")
