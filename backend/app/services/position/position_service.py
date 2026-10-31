from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SimPosition, SimPositionEvent, SimPositionSettings
from app.services.market_data import MarketDataService
from app.services.position.calculations import (
    apply_slippage,
    compute_metrics,
    fee_on_quantity,
    gross_pnl,
    position_notional,
    quantity_from_notional,
)
from app.services.position.execution_adapters import ExecutionResult, OrderRequest
from app.services.position.settings_service import (
    SettingsValidationError,
    fee_profile_from_settings,
    get_or_create_settings,
    validate_order_request_fields,
)
from app.services.position import wallet_service as wallet

logger = logging.getLogger(__name__)


class PositionServiceError(Exception):
    pass


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
        try:
            validate_order_request_fields(
                direction=req.direction,
                leverage=req.leverage,
                margin_usdt=req.margin_usdt,
                order_type=req.order_type,
                entry_price=req.entry_price,
                stop_loss=req.stop_loss,
                take_profit=req.take_profit,
            )
        except SettingsValidationError as e:
            raise PositionServiceError(str(e)) from e
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

    async def _enforce_max_risk(
        self,
        session: AsyncSession,
        settings: SimPositionSettings,
        req: OrderRequest,
        entry_price: float,
    ) -> None:
        summary = await wallet.wallet_summary(session)
        equity = max(summary["wallet_balance"], 0.01)
        notional = position_notional(req.margin_usdt, req.leverage)
        qty = quantity_from_notional(notional, entry_price)
        sl_loss = abs(gross_pnl(req.direction, entry_price, req.stop_loss, qty))
        risk_pct = sl_loss / equity * 100.0
        if risk_pct > settings.max_risk_pct_per_position + 1e-9:
            raise PositionServiceError(
                f"ریسک تا SL ({risk_pct:.2f}%) از سقف {settings.max_risk_pct_per_position}% بیشتر است"
            )

    async def create_order(
        self, session: AsyncSession, req: OrderRequest, *, commit: bool = True
    ) -> ExecutionResult:
        if req.ai_setup_id:
            from app.models import AiProposedSetup
            from app.services.entry_strategies import strategy_enabled_error
            setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == req.ai_setup_id))).scalar_one_or_none()
            if setup:
                err = await strategy_enabled_error(session, setup.structured_snapshot or {})
                if err:
                    raise PositionServiceError(err)
        settings = await get_or_create_settings(session)
        await self._validate_order(settings, req)
        summary = await wallet.wallet_summary(session)
        if req.margin_usdt > summary["available_balance"]:
            raise PositionServiceError("موجودی آزاد کافی نیست")

        order_type = req.order_type.lower()
        entry_for_risk = req.entry_price
        if order_type == "market":
            mkt, _ = await self.market.fetch_ticker(req.symbol or "BTCUSDT")
            entry_for_risk = mkt
            if req.direction == "LONG" and not (req.stop_loss < mkt < req.take_profit):
                raise PositionServiceError("سطوح LONG با قیمت بازار سازگار نیست")
            if req.direction == "SHORT" and not (req.take_profit < mkt < req.stop_loss):
                raise PositionServiceError("سطوح SHORT با قیمت بازار سازگار نیست")
        if entry_for_risk is not None:
            await self._enforce_max_risk(session, settings, req, entry_for_risk)

        entry_for_tp_cap = entry_for_risk
        if order_type == "limit" and req.entry_price is not None:
            entry_for_tp_cap = float(req.entry_price)
        if entry_for_tp_cap is None:
            raise PositionServiceError("قیمت مرجع برای TP اجرایی در دسترس نیست")

        from app.domain.execution_tp_cap import apply_execution_take_profit_cap
        from app.services.execution_tp_cap_apply import build_execution_tp_extra

        tp_cap = apply_execution_take_profit_cap(
            direction=req.direction,
            entry=float(entry_for_tp_cap),
            stop_loss=req.stop_loss,
            take_profit_ai=req.take_profit,
            take_profit_2_ai=req.take_profit_2,
        )
        execution_tp = tp_cap.execution_take_profit
        if req.direction == "LONG" and not (req.stop_loss < entry_for_tp_cap < execution_tp):
            raise PositionServiceError("TP اجرایی پس از محدودیت R:R اجرایی با سطوح LONG ناسازگار است")
        if req.direction == "SHORT" and not (execution_tp < entry_for_tp_cap < req.stop_loss):
            raise PositionServiceError("TP اجرایی پس از محدودیت R:R اجرایی با سطوح SHORT ناسازگار است")

        # Defense in depth: AI orders must satisfy the hard net R:R floor
        # AFTER the execution TP cap and with actual paper fee settings.
        if req.ai_setup_id:
            from app.domain.paper_execution_policy import HARD_MIN_AUTO_PAPER_NET_RR
            from app.services.execution_model_sim import auto_paper_rr_from_settings

            rr = auto_paper_rr_from_settings(
                settings, direction=req.direction, entry=float(entry_for_tp_cap),
                sl=req.stop_loss, tp=req.take_profit,
                min_net_rr=HARD_MIN_AUTO_PAPER_NET_RR,
            )
            if not rr.acceptable:
                raise PositionServiceError(
                    f"execution_net_rr_below_min: {rr.net_rr:.3f} < {HARD_MIN_AUTO_PAPER_NET_RR:.2f}"
                )

        public_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        monitor_from_ms = int(now.timestamp() * 1000)

        limit_price = req.entry_price if order_type == "limit" else None

        order_extra = dict(req.order_extra or {})
        order_extra.update(
            build_execution_tp_extra(
                tp_cap=tp_cap,
                entry_ref=float(entry_for_tp_cap),
                direction=req.direction,
                stop_loss=req.stop_loss,
                now=now,
            )
        )

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
            take_profit=execution_tp,
            ai_setup_id=req.ai_setup_id,
            monitor_from_ms=monitor_from_ms,
            entry_is_maker=order_type == "limit",
            extra=order_extra,
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
        await _log_event(
            session,
            pos,
            "ORDER_CREATED",
            None,
            pos.status,
            {
                "order_type": order_type,
                "ai_original_take_profit": tp_cap.ai_take_profit,
                "execution_take_profit": execution_tp,
                "execution_tp_cap_capped": tp_cap.was_capped,
            },
        )

        if order_type == "market":
            price, _ = await self.market.fetch_ticker(pos.symbol)
            filled_ok = await self._fill_entry(session, pos, settings, price, transition_suffix="market_open")
            if not filled_ok:
                if commit:
                    await session.commit()
                return ExecutionResult(
                    False,
                    public_id,
                    "ورود Market به‌دلیل Slippage/هندسه نامعتبر لغو شد",
                    {"status": pos.status, "reason": "fill_geometry_invalid"},
                )

        if commit:
            await session.commit()
        return ExecutionResult(True, public_id, "سفارش ثبت شد", {"status": pos.status})

    async def _fill_entry(
        self,
        session: AsyncSession,
        pos: SimPosition,
        settings: SimPositionSettings,
        raw_price: float,
        transition_suffix: str,
    ) -> bool:
        if pos.status not in ("pending_entry",):
            return pos.status == "open"
        if pos.ai_setup_id:
            from app.models import AiProposedSetup
            from app.services.entry_strategies import strategy_enabled_error
            setup = (await session.execute(select(AiProposedSetup).where(AiProposedSetup.setup_id == pos.ai_setup_id))).scalar_one_or_none()
            if setup:
                err = await strategy_enabled_error(session, setup.structured_snapshot or {})
                if err:
                    await self.cancel_pending(session, pos.public_id, commit=False)
                    pos.extra = {**(pos.extra or {}), "auto_paper_cancel_reason": err}
                    return False
        fees = fee_profile_from_settings(settings)
        adverse = pos.order_type == "market"
        fill = apply_slippage(
            raw_price, pos.direction, side="entry", adverse=adverse, bps=settings.slippage_bps
        )
        key = f"entry:{pos.public_id}:{transition_suffix}:{int(fill * 100)}"
        if pos.transition_key:
            return pos.status == "open"

        from app.services.execution_tp_cap_apply import (
            compute_post_fill_execution_tp,
            execution_tp_cap_enabled,
        )

        cap_result = compute_post_fill_execution_tp(pos, fill_price=fill)
        if execution_tp_cap_enabled(pos.extra) and not cap_result.geometry_ok:
            await self._reject_pending_fill_geometry(
                session,
                pos,
                fill_price=fill,
                reason=cap_result.geometry_reason or "fill_geometry_invalid",
            )
            return False

        if (pos.extra or {}).get("auto_paper"):
            from app.domain.paper_execution_policy import HARD_MIN_AUTO_PAPER_NET_RR
            from app.services.execution_model_sim import auto_paper_rr_from_settings

            rr_after_fill = auto_paper_rr_from_settings(
                settings, direction=pos.direction, entry=fill,
                sl=pos.stop_loss, tp=cap_result.take_profit,
                min_net_rr=HARD_MIN_AUTO_PAPER_NET_RR,
                entry_fill_price=fill,
            )
            if not rr_after_fill.acceptable:
                await self._reject_pending_fill_geometry(
                    session, pos, fill_price=fill,
                    reason="post_fill_net_rr_below_min",
                )
                pos.extra = {
                    **(pos.extra or {}),
                    "post_fill_net_rr": rr_after_fill.net_rr,
                    "required_min_net_rr": HARD_MIN_AUTO_PAPER_NET_RR,
                }
                return False

        notional = position_notional(pos.margin_usdt, pos.leverage)
        qty = quantity_from_notional(notional, fill)
        rate = fees.maker_rate if pos.entry_is_maker else fees.taker_rate
        open_fee = fee_on_quantity(qty, fill, rate)

        pos.transition_key = key
        pos.quantity_btc = qty
        pos.fill_price = fill
        pos.entry_price = fill
        pos.entry_fee_usdt = open_fee
        pos.take_profit = cap_result.take_profit
        pos.extra = cap_result.extra
        pos.status = "open"
        pos.opened_at = datetime.now(timezone.utc)
        pos.last_processed_candle_ms = pos.monitor_from_ms or 0
        tp_tightened = cap_result.applied and cap_result.tp_changed

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
            {
                "fill_price": fill,
                "open_fee": open_fee,
                "execution_tp_post_fill_adjusted": tp_tightened,
                "take_profit": pos.take_profit,
            },
        )
        if (pos.extra or {}).get("auto_paper"):
            from app.services.paper_auto_trading import mark_setup_paper_fill

            await mark_setup_paper_fill(session, pos)
            logger.info(
                "PAPER POSITION OPENED",
                extra={
                    "position_id": pos.public_id,
                    "setup_id": pos.ai_setup_id,
                    "fill_price": fill,
                },
            )
        elif pos.ai_setup_id:
            from app.domain.setup_lifecycle import POSITION_OPEN
            from app.models import AiProposedSetup

            setup = (
                await session.execute(
                    select(AiProposedSetup).where(AiProposedSetup.setup_id == pos.ai_setup_id)
                )
            ).scalar_one_or_none()
            if setup:
                setup.lifecycle_state = POSITION_OPEN
        return True

    async def _reject_pending_fill_geometry(
        self,
        session: AsyncSession,
        pos: SimPosition,
        *,
        fill_price: float,
        reason: str,
    ) -> None:
        """Cancel pending order when fill price breaks SL/Entry/TP ordering (no open fee)."""
        extra = dict(pos.extra or {})
        extra.update(
            {
                "fill_rejected_at": datetime.now(timezone.utc).isoformat(),
                "fill_rejected_reason": reason,
                "fill_attempt_price": float(fill_price),
            }
        )
        pos.extra = extra
        await wallet.append_ledger(
            session,
            entry_type=wallet.LEDGER_MARGIN_RELEASE,
            amount_usdt=pos.margin_usdt,
            reference_type="sim_position",
            reference_id=pos.public_id,
            note="لغو — اعتبارسنجی Fill ناموفق",
            affects_balance=False,
        )
        await _log_event(
            session,
            pos,
            "FILL_REJECTED",
            pos.status,
            "cancelled",
            {"reason": reason, "fill_price": fill_price},
        )
        pos.status = "cancelled"
        pos.close_reason = reason
        pos.closed_at = datetime.now(timezone.utc)

    async def cancel_pending(
        self, session: AsyncSession, public_id: str, *, commit: bool = True
    ) -> ExecutionResult:
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
        if commit:
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
        ambiguous_policy: str = "bar_close",
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
        exit_fill = apply_slippage(
            raw_exit, pos.direction, side="exit", adverse=exit_adverse, bps=settings.slippage_bps
        )
        entry = pos.fill_price or pos.entry_price or raw_exit
        qty = pos.quantity_btc
        if qty is None or qty <= 0:
            notional = position_notional(pos.margin_usdt, pos.leverage)
            qty = quantity_from_notional(notional, entry)
            pos.quantity_btc = qty
        exit_rate = fees.maker_rate if exit_is_maker else fees.taker_rate
        exit_fee = fee_on_quantity(qty, exit_fill, exit_rate)
        gross = gross_pnl(pos.direction, entry, exit_fill, qty)
        net = gross - pos.entry_fee_usdt - exit_fee

        pos.exit_price = exit_fill
        pos.exit_fee_usdt = exit_fee
        pos.gross_pnl_usdt = gross
        pos.net_pnl_usdt = net
        if ambiguous:
            pos.close_reason = "ambiguous_intrabar"
            pos.status = "closed"
            pos.execution_state = "normal"
            pos.extra = {
                **pos.extra,
                "ambiguous_intrabar": True,
                "outcome_valid": False,
                "settlement_policy": ambiguous_policy,
                "settlement_price_raw": raw_exit,
            }
        else:
            pos.close_reason = reason
            pos.status = reason if reason in ("tp_hit", "sl_hit", "liquidated") else "closed"
        pos.closed_at = datetime.now(timezone.utc)

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
            amount_usdt=gross,
            reference_type="sim_position",
            reference_id=pos.public_id,
            note="gross price PnL",
        )
        await _log_event(
            session,
            pos,
            "POSITION_CLOSED",
            "open",
            pos.status,
            {"reason": pos.close_reason, "gross_pnl": gross, "net_pnl": net, "bar_ms": bar_ms},
        )
        if (pos.extra or {}).get("auto_paper"):
            if pos.close_reason == "tp_hit":
                logger.info(
                    "PAPER TP HIT",
                    extra={
                        "position_id": pos.public_id,
                        "setup_id": pos.ai_setup_id,
                        "net_pnl_usdt": net,
                    },
                )
            elif pos.close_reason == "sl_hit":
                logger.info(
                    "PAPER SL HIT",
                    extra={
                        "position_id": pos.public_id,
                        "setup_id": pos.ai_setup_id,
                        "net_pnl_usdt": net,
                    },
                )
            logger.info(
                "PAPER AUTO RESULT",
                extra={
                    "position_id": pos.public_id,
                    "setup_id": pos.ai_setup_id,
                    "close_reason": pos.close_reason,
                    "net_pnl_usdt": net,
                },
            )
        from app.services.position.equity_service import record_equity_snapshot

        await record_equity_snapshot(session, self)

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
            exit_is_maker=(
                settings.default_tp_behavior == "limit"
                if pos.status in ("pending_entry", "open")
                else pos.exit_is_maker
            ),
            slippage_bps=settings.slippage_bps,
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
            "execution_take_profit": pos.take_profit,
            "ai_original_take_profit": (pos.extra or {}).get("ai_original_take_profit"),
            "ai_original_take_profit_2": (pos.extra or {}).get("ai_original_take_profit_2"),
            "execution_take_profit_2": (pos.extra or {}).get("execution_take_profit_2"),
            "execution_tp_cap_capped": (pos.extra or {}).get("execution_tp_cap_capped"),
            "mark_price": mark_price,
            "ai_setup_id": pos.ai_setup_id,
            "quantity_btc": pos.quantity_btc,
            "execution_state": pos.execution_state,
            "metrics": metrics.__dict__,
            "opened_at_tehran": None,
        }
