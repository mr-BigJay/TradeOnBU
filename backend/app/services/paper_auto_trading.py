from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiProposedSetup, SimPosition, SimPositionSettings
from app.services.ai_setup_memory import validate_proposed_setup_levels
from app.services.position.execution_adapters import OrderRequest
from app.services.position.position_service import PositionServiceError, SimPositionService
from app.services.position.settings_service import get_or_create_settings
from app.services.runtime_settings import get_runtime_settings

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def setup_eligible_for_auto_entry(setup: AiProposedSetup, *, now: datetime | None = None) -> tuple[bool, str | None]:
    now = now or _utc_now()
    if setup.status != "open":
        return False, f"setup_status_{setup.status}"
    if setup.expires_at is not None and setup.expires_at <= now:
        return False, "setup_expired"
    entry = setup.entry_price
    sl = setup.stop_loss
    tp = setup.take_profit
    if entry is None or sl is None or tp is None:
        return False, "missing_levels"
    ok, err = validate_proposed_setup_levels(setup.direction, entry, sl, tp)
    if not ok:
        return False, err
    if setup.paper_fill_recorded:
        return False, "paper_fill_already_recorded"
    return True, None


async def _active_position_count(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(SimPosition)
        .where(SimPosition.status.in_(("pending_entry", "open")))
    )
    return int(result.scalar_one())


async def _has_position_for_setup(session: AsyncSession, setup_id: str) -> bool:
    result = await session.execute(
        select(SimPosition.id)
        .where(SimPosition.ai_setup_id == setup_id)
        .where(SimPosition.status.in_(("pending_entry", "open")))
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


def _validation_allows_auto_paper(validation: dict[str, Any]) -> tuple[bool, str | None]:
    decision = str(validation.get("ai_decision") or "").upper()
    if decision not in ("LONG", "SHORT"):
        return False, "not_trade_decision"
    if validation.get("acceptable_rr") is not True:
        return False, "net_rr_not_acceptable"
    return True, None


async def maybe_auto_open_paper_position(
    session: AsyncSession,
    *,
    run_id: int,
    setup: AiProposedSetup,
    validation: dict[str, Any],
    symbol: str,
    commit: bool = True,
) -> dict[str, Any]:
    """
    Create a pending limit paper order from a validated AI setup when auto mode is enabled.
    Live trading is never used.
    """
    meta: dict[str, Any] = {"attempted": True, "created": False, "skipped": False}

    settings: SimPositionSettings = await get_or_create_settings(session)
    if not settings.auto_open_paper_positions:
        meta["skipped"] = True
        meta["reason"] = "auto_open_disabled"
        return meta

    runtime = await get_runtime_settings(session)
    if runtime.paper_trading_paused:
        meta["skipped"] = True
        meta["reason"] = "paper_trading_paused"
        return meta

    if not settings.initial_balance_set:
        meta["skipped"] = True
        meta["reason"] = "wallet_not_initialized"
        return meta

    ok_val, val_reason = _validation_allows_auto_paper(validation)
    if not ok_val:
        meta["skipped"] = True
        meta["reason"] = val_reason
        return meta

    ok_setup, setup_reason = setup_eligible_for_auto_entry(setup)
    if not ok_setup:
        meta["skipped"] = True
        meta["reason"] = setup_reason
        return meta

    if await _has_position_for_setup(session, setup.setup_id):
        meta["skipped"] = True
        meta["reason"] = "position_already_active_for_setup"
        return meta

    if await _active_position_count(session) >= runtime.max_simultaneous_positions:
        meta["skipped"] = True
        meta["reason"] = "max_simultaneous_positions"
        return meta

    entry = float(setup.entry_price)  # type: ignore[arg-type]
    sl = float(setup.stop_loss)  # type: ignore[arg-type]
    tp = float(setup.take_profit)  # type: ignore[arg-type]
    direction = (setup.direction or "").upper()

    svc = SimPositionService()
    req = OrderRequest(
        symbol=symbol,
        direction=direction,
        margin_usdt=settings.default_margin_usdt,
        leverage=settings.default_leverage,
        order_type="limit",
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        ai_setup_id=setup.setup_id,
    )
    try:
        result = await svc.create_order(session, req, commit=False)
    except PositionServiceError as exc:
        meta["skipped"] = True
        meta["reason"] = "order_rejected"
        meta["detail"] = str(exc)
        return meta

    if not result.success or not result.position_id:
        meta["skipped"] = True
        meta["reason"] = "order_failed"
        meta["detail"] = result.message
        return meta

    pos = await svc.get_position_by_public_id(session, result.position_id)
    if pos:
        pos.extra = {
            **pos.extra,
            "auto_paper": True,
            "ai_run_id": run_id,
            "source": "gpt_auto_paper",
        }
        await session.flush()

    meta["created"] = True
    meta["position_id"] = result.position_id
    meta["setup_id"] = setup.setup_id
    logger.info(
        "PAPER AUTO ENTRY CREATED",
        extra={
            "position_id": result.position_id,
            "setup_id": setup.setup_id,
            "run_id": run_id,
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
        },
    )

    if commit:
        await session.commit()
    return meta


async def auto_paper_entry_still_valid(session: AsyncSession, pos: SimPosition) -> tuple[bool, str | None]:
    if not (pos.extra or {}).get("auto_paper"):
        return True, None
    if not pos.ai_setup_id:
        return False, "missing_ai_setup_id"
    setup = (
        await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == pos.ai_setup_id))
    ).scalar_one_or_none()
    if setup is None:
        return False, "setup_not_found"
    return setup_eligible_for_auto_entry(setup)


async def mark_setup_paper_fill(session: AsyncSession, pos: SimPosition) -> None:
    if not pos.ai_setup_id:
        return
    setup = (
        await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == pos.ai_setup_id))
    ).scalar_one_or_none()
    if setup:
        setup.paper_fill_recorded = True
