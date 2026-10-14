from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SimPosition, SimPositionEvent, SimPositionSettings
from app.services.market_data import MarketDataService
from app.services.position.calculations import (
    compute_metrics,
    fee_on_notional,
    gross_pnl,
    position_notional,
    quantity_from_notional,
)
from app.services.position.execution_adapters import ExecutionResult, OrderRequest
from app.services.position.settings_service import fee_profile_from_settings, get_or_create_settings
from app.services.position import wallet_service as wallet


class PositionServiceError(Exception):
    pass


def _apply_slippage(price: float, direction: str, adverse: bool, bps: float) -> float:
    factor = bps / 10000.0
    if direction == "LONG":
        return price * (1 + factor) if adverse else price * (1 - factor)
    return price * (1 - factor) if adverse else price * (1 + factor)


async def _log_event(
    session: AsyncSession,
    pos: SimPosition,
    event_type: str,
    from_status: str | None,
    to_status: str | None,
    payload: dict | None = None,
    note: str | None = None,
) -> None:
    session.add(
        SimPositionEvent(
            position_id=pos.id,
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            payload=payload or {},
            note=note,
        )
    )


class SimPositionService:
    def __init__(self, market: MarketDataService | None = None) -> None:
        self.market = market or MarketDataService()

    async def get_position_by_public_id(self, session: AsyncSession, public_id: str) -> SimPosition | None:
        result = await session.execute(select(SimPosition).where(SimPosition.public_id == public_id))
        return result.scalar_one_or_none()

    async def list_active(self, session: AsyncSession) -> list[SimPosition]:
        result = await session.execute(
            select(SimPosition)
            .where(SimPosition.status.in_(("pending_entry", "open")))
            .order_by(SimPosition.created_at.desc())
        )
        return list(result.scalars().all())

    async def list_all(self, session: AsyncSession, limit: int = 100) -> list[SimPosition]:
        result = await session.execute(select(SimPosition).order_by(SimPosition.created_at.desc()).limit(limit))
        return list(result.scalars().all())

    async def _validate_order(
        self, settings: SimPositionSettings, req: OrderRequest
    ) -> None:
        if req.direction not in ("LONG", "SHORT"):
            raise PositionServiceError("جهت نامعتبر")
        if req.leverage < 1 or req.leverage > 125:
            raise PositionServiceError("اهرم خارج از محدوده")
        if req.margin_usdt <= 0:
            raise PositionServiceError("مارجین باید مثبت باشد")
        if req.margin_usdt > settings.max_margin_per_position:
            raise PositionServiceError("مارجین از سقف مجاز بیشتر است")
        if req.order_type == "limit" and not req.entry_price:
            raise PositionServiceError("قیمت ورود برای لیمیت لازم است")
        entry_ref = req.entry_price
        if req.order_type == "market":
            pass
        elif entry_ref is None:
            raise PositionServiceError("قیمت ورود نامعتبر")
        else:
            if req.direction == "LONG" and not (req.stop_loss < entry_ref < req.take_profit):
                raise PositionServiceError("سطوح LONG نامعتبر")
            if req.direction == "SHORT" and not (req.take_profit < entry_ref < req.stop_loss):
                raise PositionServiceError("سطوح SHORT نامعتبر")

    async def create_order(self, session: AsyncSession, req: OrderRequest) -> ExecutionResult:
        settings = await get_or_create_settings(session)
        await self._validate_order(settings, req)
        summary = await wallet.wallet_summary(session)
        if req.margin_usdt > summary["available_balance"]:
            raise PositionServiceError("موجودی آزاد کافی نیست")

        order_type = req.order_type.lower()
        if order_type == "market":
            mkt, _ = await self.market.fetch_ticker(req.symbol or "BTCUSDT")
            if req.direction == "LONG" and not (req.stop_loss < mkt < req.take_profit):
                raise PositionServiceError("سطوح LONG با قیمت بازار سازگار نیست")
            if req.direction == "SHORT" and not (req.take_profit < mkt < req.stop_loss):
                raise PositionServiceError("سطوح SHORT با قیمت بازار سازگار نیست")

        public_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        monitor_from_ms = int(now.timestamp() * 1000)

        limit_price = req.entry_price if order_type == "limit" else None

        pos = SimPosition(
            public_id=public_id,
            symbol=req.symbol or "BTCUSDT",
            direction=req.direction,
            status="pending_entry" if order_type == "limit" else "pending_entry",
            order_type=order_type,
            margin_usdt=req.margin_usdt,
            leverage=req.leverage,
            limit_entry_price=limit_price,
            entry_price=limit_price,
            stop_loss=req.stop_loss,
            take_profit=req.take_profit,
            ai_setup_id=req.ai_setup_id,
            monitor_from_ms=monitor_from_ms,
            entry_is_maker=order_type == "limit",
        )
        session.add(pos)
        await session.flush()

        await wallet.append_ledger(
            session,
            entry_type=wallet.LEDGER_MARGIN_RESERVE,
            amount_usdt=req.margin_usdt,
            reference_type="sim_position",
            reference_id=public_id,
            note="رزرو مارجین",
            affects_balance=False,
        )
        await _log_event(session, pos, "ORDER_CREATED", None, pos.status, {"order_type": order_type})

        if order_type == "market":
            price, _ = await self.market.fetch_ticker(pos.symbol)
            await self._fill_entry(session, pos, settings, price, transition_suffix="market_open")

        await session.commit()
        return ExecutionResult(True, public_id, "سفارش ثبت شد", {"status": pos.status})

    async def _fill_entry(
        self,
        session: AsyncSession,
        pos: SimPosition,
        settings: SimPositionSettings,
        raw_price: float,
        transition_suffix: str,
    ) -> None:
        if pos.status not in ("pending_entry",):
            return
        fees = fee_profile_from_settings(settings)
        adverse = pos.order_type == "market"
        fill = _apply_slippage(raw_price, pos.direction, adverse=adverse, bps=settings.slippage_bps)
        notional = position_notional(pos.margin_usdt, pos.leverage)
        rate = fees.maker_rate if pos.entry_is_maker else fees.taker_rate
        open_fee = fee_on_notional(notional, fill, rate)
        key = f"entry:{pos.public_id}:{transition_suffix}:{int(fill * 100)}"
        if pos.transition_key:
            return
        pos.transition_key = key
        pos.fill_price = fill
        pos.entry_price = fill
        pos.entry_fee_usdt = open_fee
        pos.status = "open"
        pos.opened_at = datetime.now(timezone.utc)
        pos.last_processed_candle_ms = pos.monitor_from_ms or 0

        await wallet.append_ledger(
            session,
            entry_type=wallet.LEDGER_OPEN_FEE,
            amount_usdt=-open_fee,
            reference_type="sim_position",
            reference_id=pos.public_id,
        )
        await _log_event(
            session,
            pos,
            "ENTRY_FILLED",
            "pending_entry",
            "open",
            {"fill_price": fill, "open_fee": open_fee},
        )

    async def cancel_pending(self, session: AsyncSession, public_id: str) -> ExecutionResult:
        pos = await self.get_position_by_public_id(session, public_id)
        if not pos or pos.status != "pending_entry":
            return ExecutionResult(False, public_id, "سفارش در انتظار یافت نشد", {})
        await wallet.append_ledger(
            session,
            entry_type=wallet.LEDGER_MARGIN_RELEASE,
            amount_usdt=pos.margin_usdt,
            reference_type="sim_position",
            reference_id=public_id,
            note="لغو سفارش",
            affects_balance=False,
        )
        await _log_event(session, pos, "ORDER_CANCELLED", pos.status, "cancelled")
        pos.status = "cancelled"
        pos.closed_at = datetime.now(timezone.utc)
        await session.commit()
        return ExecutionResult(True, public_id, "لغو شد", {})

    async def manual_close(self, session: AsyncSession, public_id: str) -> ExecutionResult:
        pos = await self.get_position_by_public_id(session, public_id)
        if not pos or pos.status != "open":
            return ExecutionResult(False, public_id, "پوزیشن باز یافت نشد", {})
        settings = await get_or_create_settings(session)
        price, _ = await self.market.fetch_ticker(pos.symbol)
        await self._close_position(
            session,
            pos,
            settings,
            raw_exit=price,
            reason="manual_close",
            exit_adverse=True,
            bar_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
        )
        await session.commit()
        return ExecutionResult(True, public_id, "بسته شد", {})

    async def _close_position(
        self,
        session: AsyncSession,
        pos: SimPosition,
        settings: SimPositionSettings,
        *,
        raw_exit: float,
        reason: str,
        exit_adverse: bool,
        bar_ms: int,
        ambiguous: bool = False,
    ) -> None:
        if pos.status != "open":
            return
        key = f"exit:{pos.public_id}:{reason}:{bar_ms}"
        if pos.transition_key and pos.transition_key.startswith("exit:"):
            return
        pos.transition_key = key

        fees = fee_profile_from_settings(settings)
        exit_is_maker = reason in ("tp_hit",) and settings.default_tp_behavior == "limit"
        pos.exit_is_maker = exit_is_maker
        exit_fill = _apply_slippage(raw_exit, pos.direction, adverse=exit_adverse, bps=settings.slippage_bps)
        notional = position_notional(pos.margin_usdt, pos.leverage)
        entry = pos.fill_price or pos.entry_price or raw_exit
        qty = quantity_from_notional(notional, entry)
        exit_rate = fees.maker_rate if exit_is_maker else fees.taker_rate
        exit_fee = fee_on_notional(notional, exit_fill, exit_rate)
        gross = gross_pnl(pos.direction, entry, exit_fill, qty)
        net = gross - pos.entry_fee_usdt - exit_fee

        pos.exit_price = exit_fill
        pos.exit_fee_usdt = exit_fee
        pos.gross_pnl_usdt = gross
        pos.net_pnl_usdt = net
        pos.close_reason = "ambiguous_intrabar" if ambiguous else reason
        pos.status = reason if reason in ("tp_hit", "sl_hit", "liquidated") else "closed"
        if pos.status == "closed":
            pass
        pos.closed_at = datetime.now(timezone.utc)
        if ambiguous:
            pos.extra = {**pos.extra, "ambiguous_intrabar": True}

        await wallet.append_ledger(
            session,
            entry_type=wallet.LEDGER_CLOSE_FEE,
            amount_usdt=-exit_fee,
            reference_type="sim_position",
            reference_id=pos.public_id,
        )
        await wallet.append_ledger(
            session,
            entry_type=wallet.LEDGER_MARGIN_RELEASE,
            amount_usdt=pos.margin_usdt,
            reference_type="sim_position",
            reference_id=pos.public_id,
            affects_balance=False,
        )
        await wallet.append_ledger(
            session,
            entry_type=wallet.LEDGER_REALIZED_PNL,
            amount_usdt=net,
            reference_type="sim_position",
            reference_id=pos.public_id,
        )
        await _log_event(
            session,
            pos,
            "POSITION_CLOSED",
            "open",
            pos.status,
            {"reason": pos.close_reason, "net_pnl": net, "bar_ms": bar_ms},
        )

    async def enrich_open(
        self,
        session: AsyncSession,
        pos: SimPosition,
        mark_price: float | None = None,
    ) -> dict:
        settings = await get_or_create_settings(session)
        fees = fee_profile_from_settings(settings)
        entry = pos.fill_price or pos.entry_price or pos.limit_entry_price
        if not entry:
            return {"public_id": pos.public_id, "status": pos.status}
        if mark_price is None:
            mark_price, _ = await self.market.fetch_ticker(pos.symbol)
        metrics = compute_metrics(
            direction=pos.direction,
            margin_usdt=pos.margin_usdt,
            leverage=pos.leverage,
            entry_price=entry,
            mark_price=mark_price,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            fees=fees,
            entry_is_maker=pos.entry_is_maker,
            exit_is_maker=pos.exit_is_maker,
        )
        return {
            "public_id": pos.public_id,
            "symbol": pos.symbol,
            "direction": pos.direction,
            "status": pos.status,
            "order_type": pos.order_type,
            "margin_usdt": pos.margin_usdt,
            "leverage": pos.leverage,
            "entry_price": entry,
            "limit_entry_price": pos.limit_entry_price,
            "fill_price": pos.fill_price,
            "stop_loss": pos.stop_loss,
            "take_profit": pos.take_profit,
            "mark_price": mark_price,
            "ai_setup_id": pos.ai_setup_id,
            "metrics": metrics.__dict__,
            "opened_at_tehran": None,
        }
