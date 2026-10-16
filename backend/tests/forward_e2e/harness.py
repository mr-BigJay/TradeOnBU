"""Scripted market + timeline helpers for forward paper E2E (real monitor/position code)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.candle_utils import candle_close_ms
from app.domain.candles import Candle
from app.domain.setup_monitor_boundary import first_eligible_bar_open_ms
from app.models import AiProposedSetup, SimPosition, SimPositionSettings
from app.services.market_data import KlineBundle
from app.services.opportunity_monitor import OpportunityMonitorService
from app.services.position import wallet_service as wallet
from app.services.position.position_monitor import SimPositionMonitorService

INTERVAL_5M = 300_000
INTERVAL_1M = 60_000


def bar_5m(open_ms: int, *, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(time_ms=open_ms, open=o, high=h, low=l, close=c, volume=1.0)


def bar_1m(open_ms: int, *, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(time_ms=open_ms, open=o, high=h, low=l, close=c, volume=1.0)


def sweep_mss_poi_long_bars(boundary_ms: int) -> list[Candle]:
    bars: list[Candle] = []
    for i in range(14, 0, -1):
        open_ms = boundary_ms - i * INTERVAL_5M
        bars.append(bar_5m(open_ms, o=100.0, h=100.3, l=99.0, c=100.1))
    bars.append(bar_5m(boundary_ms, o=100.0, h=100.2, l=98.6, c=99.4))
    bars.append(bar_5m(boundary_ms + INTERVAL_5M, o=99.4, h=100.8, l=99.2, c=100.7))
    bars.append(bar_5m(boundary_ms + 2 * INTERVAL_5M, o=100.5, h=100.6, l=99.8, c=100.2))
    return bars


def sweep_mss_poi_short_bars(boundary_ms: int) -> list[Candle]:
    bars: list[Candle] = []
    for i in range(14, 0, -1):
        open_ms = boundary_ms - i * INTERVAL_5M
        bars.append(bar_5m(open_ms, o=100.0, h=100.5, l=99.5, c=100.2))
    bars.append(bar_5m(boundary_ms, o=100.0, h=101.4, l=99.8, c=100.3))
    bars.append(bar_5m(boundary_ms + INTERVAL_5M, o=100.3, h=100.4, l=98.9, c=99.1))
    bars.append(bar_5m(boundary_ms + 2 * INTERVAL_5M, o=99.5, h=100.2, l=99.7, c=100.0))
    return bars


def production_conditional_wait_short() -> dict[str, Any]:
    return {
        "decision": "NO_TRADE",
        "final_ai_decision": "WAIT",
        "setup_mode": "conditional",
        "potential_direction": "SHORT",
        "conditional_entry_zone_low": 99.0,
        "conditional_entry_zone_high": 101.0,
        "confirmation_required": "BSL sweep then 5m bearish MSS into POI",
        "confirmation_type": "sweep_mss_poi",
        "conditional_invalidation": "5m close above structural invalidation",
        "structural_invalidation_level": 108.0,
        "stop_loss": 106.0,
        "take_profit": 85.0,
        "risk_reward": 2.2,
        "current_price": 100.0,
        "market_bias": "Bearish intraday pullback",
        "setup_timeframe": "intraday_5m_15m",
        "liquidity_event_clear": False,
        "poi_reaction_clear": False,
        "structural_confirmation_clear": False,
        "logical_invalidation_clear": True,
        "ict_liquidity": "BSL pool above 101",
        "ict_structure": "Awaiting sweep + MSS",
        "trade_reason": "Conditional short after liquidity run",
        "report_fa": "",
        "proposed_setup": {"new_setup": True, "thesis_fa": "Wait for ICT sequence"},
    }


@dataclass
class ScenarioTimeline:
    scenario_id: str
    decision_reason: str
    events: list[dict[str, Any]] = field(default_factory=list)
    delays_ms: dict[str, int | None] = field(default_factory=dict)

    def add(self, kind: str, *, at_ms: int | None = None, detail: str | None = None, **extra: Any) -> None:
        self.events.append({"kind": kind, "at_ms": at_ms, "detail": detail, **extra})

    def to_report(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "decision_reason": self.decision_reason,
            "events": self.events,
            "delays_ms": self.delays_ms,
        }


class ScriptedMarket:
    def __init__(self) -> None:
        self.closed_5m: list[Candle] = []
        self.closed_1m: list[Candle] = []
        self.forming_5m: Candle | None = None
        self._5m_complete = True
        self._1m_complete = True
        self._5m_gaps: list[int] = []
        self._1m_gaps: list[int] = []

    def bundle(self, interval: str, *, limit: int = 200) -> KlineBundle:
        if interval == "5m":
            closed = self.closed_5m[-limit:]
            return KlineBundle(
                closed=closed,
                forming=self.forming_5m,
                gaps_detected=[],
                gaps_remaining=list(self._5m_gaps),
                data_complete=self._5m_complete,
                interval="5m",
            )
        closed = self.closed_1m[-limit:]
        return KlineBundle(
            closed=closed,
            forming=None,
            gaps_detected=[],
            gaps_remaining=list(self._1m_gaps),
            data_complete=self._1m_complete,
            interval="1m",
        )

    async def fetch_kline_bundle(self, symbol: str, interval: str, limit: int) -> KlineBundle:
        return self.bundle(interval, limit=limit)


async def enable_auto_paper(session: AsyncSession) -> SimPositionSettings:
    await wallet.append_ledger(session, entry_type=wallet.LEDGER_INITIAL, amount_usdt=50_000.0)
    settings = (await session.execute(select(SimPositionSettings))).scalar_one_or_none()
    if not settings:
        session.add(SimPositionSettings(id=1))
        await session.flush()
        settings = (await session.execute(select(SimPositionSettings))).scalar_one()
    settings.initial_balance_set = True
    settings.auto_open_paper_positions = True
    settings.max_risk_pct_per_position = 100.0
    settings.default_margin_usdt = 50.0
    settings.default_leverage = 10
    return settings


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


async def run_opportunity_monitor(
    session: AsyncSession,
    market: ScriptedMarket,
    *,
    now_ms: int,
    symbol: str = "BTCUSDT",
) -> dict[str, Any]:
    import datetime as real_datetime

    mon = OpportunityMonitorService()
    mon.market = market  # type: ignore[assignment]
    now_dt = ms_to_dt(now_ms)
    with patch("app.services.opportunity_monitor._utc_now", return_value=now_dt):
        with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 5, 600)):
            with patch("app.services.position.position_service.datetime") as pos_dt:
                pos_dt.now = lambda tz=None: now_dt
                pos_dt.side_effect = lambda *args, **kwargs: real_datetime.datetime(*args, **kwargs)
                return await mon.monitor_setups(session, symbol=symbol)


async def run_position_monitor(
    session: AsyncSession,
    market: ScriptedMarket,
    *,
    now_ms: int,
) -> int:
    import datetime as real_datetime

    sim = SimPositionMonitorService()
    sim.market = market  # type: ignore[assignment]
    now_dt = ms_to_dt(now_ms)
    with patch("app.services.opportunity_monitor._utc_now", return_value=now_dt):
        with patch("app.services.opportunity_monitor.is_market_data_fresh", return_value=(True, 5, 600)):
            with patch("app.services.position.position_service.datetime") as pos_dt:
                pos_dt.now = lambda tz=None: now_dt
                pos_dt.side_effect = lambda *args, **kwargs: real_datetime.datetime(*args, **kwargs)
                return await sim.monitor_session(session)


def enrich_timeline_from_setup(timeline: ScenarioTimeline, setup: AiProposedSetup) -> None:
    conf = setup.confirmation_state or {}
    for tok, ms in sorted((conf.get("confirmation_timestamps") or {}).items(), key=lambda x: x[1]):
        timeline.add("confirmation", at_ms=int(ms), detail=str(tok))
    if setup.lifecycle_state:
        timeline.add("lifecycle", detail=setup.lifecycle_state, at_ms=None)
    if setup.last_rejection_reason:
        timeline.add("rejection", detail=setup.last_rejection_reason)


async def fill_pending_limit_order(
    session: AsyncSession,
    market: ScriptedMarket,
    *,
    setup_id: str,
    closed_5m: list[Candle],
) -> int:
    """Drive SimPositionMonitorService until limit fill (closed 1m only, no look-ahead)."""
    from app.models import SimPosition

    pos = (
        await session.execute(select(SimPosition).where(SimPosition.ai_setup_id == setup_id))
    ).scalar_one()
    entry = float(pos.limit_entry_price or 0)
    order_ms = int(pos.monitor_from_ms or 0)
    from app.services.position.monitor_gap_utils import next_expected_bar_open_ms

    fill_open = next_expected_bar_open_ms(pos.last_processed_candle_ms or order_ms)
    hit = bar_1m(fill_open, o=entry, h=entry + 0.5, l=entry - 0.5, c=entry)
    market.closed_5m = closed_5m
    market.closed_1m = [hit]
    fill_now = candle_close_ms(hit, "1m") + 5_000
    await run_position_monitor(session, market, now_ms=fill_now)
    return fill_now


async def count_paper_orders(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(select(func.count()).select_from(SimPosition))
        ).scalar_one()
    )


def promotion_now_ms(closed_5m: list[Candle]) -> int:
    last = closed_5m[-1]
    return candle_close_ms(last, "5m") + 15_000


def setup_boundary(created: datetime) -> int:
    return first_eligible_bar_open_ms(created, "5m")
