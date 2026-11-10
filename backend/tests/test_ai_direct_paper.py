"""AI-direct paper mode: one model decision, no ICT WAIT gates, hard risk safety."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models import SimPosition
from app.services import ai_direct_paper as direct
from app.services.position.settings_service import get_or_create_settings
from app.services.position.wallet_service import append_ledger, LEDGER_INITIAL
from app.services.runtime_settings import get_runtime_settings


def _settings():
    return SimpleNamespace(
        maker_fee_rate=0.0002, taker_fee_rate=0.0005,
        slippage_bps=1, default_tp_behavior="market",
        default_margin_usdt=50, default_leverage=3,
    )


def test_direct_mode_is_opt_in_and_does_not_force_trades(monkeypatch):
    monkeypatch.delenv("AI_DIRECT_PAPER_ENTRY_ENABLED", raising=False)
    assert direct.enabled() is False
    proposal = {"decision": "NO_TRADE"}
    trade, reason = direct.preflight(
        proposal, reference_price=1000, execution_price=1000, settings=_settings()
    )
    assert trade is None and reason == "ai_decided_no_trade"


def test_ai_market_proposal_is_fee_aware_two_net_r_without_confirmation_sequence():
    proposal = {"decision": "LONG", "stop_loss": 980,
                "take_profit": 1100, "reason_fa": "بازگشت به ناحیه حمایتی"}
    trade, reason = direct.preflight(
        proposal, reference_price=1000, execution_price=1000, settings=_settings()
    )
    assert reason is None
    assert trade["direction"] == "LONG"
    assert trade["stop_loss"] == 980
    assert 1040 <= trade["take_profit"] < 1050
    assert trade["net_rr_preflight"] >= 2
    assert "confirmation" not in trade


def test_no_bad_geometry_no_large_price_chase_and_no_too_small_target():
    cases = [
        ({"decision": "LONG", "stop_loss": 1001, "take_profit": 1060}, 1000, "invalid_ai_geometry"),
        ({"decision": "SHORT", "stop_loss": 1015, "take_profit": 1060}, 1000, "invalid_ai_geometry"),
        ({"decision": "LONG", "stop_loss": 980, "take_profit": 1021}, 1000, "ai_target_below_two_net_r"),
        ({"decision": "LONG", "stop_loss": 980, "take_profit": 1100}, 1010, "price_moved_since_ai_analysis"),
    ]
    for proposal, market, expected in cases:
        result, reason = direct.preflight(
            proposal, reference_price=1000, execution_price=market, settings=_settings()
        )
        assert result is None and reason == expected




def test_direct_execution_context_is_sent_without_other_analyst_payload_changes():
    import json
    from app.services.analyst_payload import build_analyst_payload

    context = direct.execution_context(_settings(), reference_price=73_365.4)
    assert context["mode"] == "paper_market_only"
    assert context["reference_price"] == 73_365.4
    assert context["entry_fee_rate"] == 0.0005
    assert context["stop_fee_rate"] == 0.0005
    assert context["tp_fee_rate"] == 0.0005
    assert context["minimum_net_rr"] == 2.0
    assert context["execution_max_gross_rr"] == 2.5

    kwargs = dict(
        market={"symbol": "BTCUSDT", "timeframes": {}},
        evidence_catalog=[], outlook_major_levels=[],
        market_intelligence={"status": "unavailable"},
        strategy_features={"timeframes": {}},
        enabled_strategies={"ict_reversal": True},
        execution_regime={"timeframes": {}},
        instructions=direct.DIRECT_INSTRUCTIONS,
    )
    payload, audit = build_analyst_payload(
        **kwargs, direct_paper_execution=context
    )
    data = json.loads(payload)
    assert data["direct_paper_execution"] == context
    assert "direct_paper_execution" in audit["sections"]

    legacy, _ = build_analyst_payload(**kwargs)
    assert "direct_paper_execution" not in json.loads(legacy)
    assert "Never omit trade_now" in direct.DIRECT_INSTRUCTIONS


def test_short_rejection_diagnostic_explains_gross_vs_net_instead_of_hiding_reason():
    # From an actual failed paper analysis (price shown in AI report,
    # not necessarily the exact post-inference market execution quote).
    s = _settings()
    s.slippage_bps = 5
    proposal = {"decision": "SHORT", "stop_loss": 73647.8,
                "take_profit": 72691.0, "reason_fa": "ساختار نزولی"}
    diagnostics = direct.rejection_diagnostics(
        proposal, execution_price=73365.4, settings=s
    )
    assert 2.3 < diagnostics["ai_target_gross_rr"] < 2.5
    assert diagnostics["ai_target_executable_net_rr"] < diagnostics["ai_target_gross_rr"]
    assert diagnostics["execution_tp_cap_gross_rr"] == 2.5
    assert diagnostics["required_net_rr"] == 2.0
    assert "net_rr_at_execution_tp_cap" in diagnostics


@pytest.mark.asyncio
async def test_ai_first_creates_one_market_paper_position_and_dedupes_run(session, monkeypatch):
    from app.services.market_data import MarketDataService
    monkeypatch.setenv("AI_DIRECT_PAPER_ENTRY_ENABLED", "1")
    monkeypatch.setenv("MULTI_SETUP_ANALYST_ENABLED", "1")
    monkeypatch.setattr(MarketDataService, "fetch_ticker", AsyncMock(return_value=(1000., None)))
    settings = await get_or_create_settings(session)
    settings.auto_open_paper_positions = True
    settings.initial_balance_set = True
    settings.default_margin_usdt = 50
    settings.default_leverage = 10
    settings.max_risk_pct_per_position = 1
    settings.maker_fee_rate = .0002
    settings.taker_fee_rate = .0005
    settings.slippage_bps = 1
    runtime = await get_runtime_settings(session)
    runtime.paper_trading_paused = False
    runtime.paper_entry_dry_run = False
    await append_ledger(session, entry_type=LEDGER_INITIAL, amount_usdt=1000)
    await session.flush()

    signal = {"decision": "LONG", "stop_loss": 980,
              "take_profit": 1100, "reason_fa": "بازگشت ساختاری 1H"}
    result = await direct.execute_direct_paper(
        session, run_id=9001, proposal=signal,
        reference_price=1000., current_price=1000.
    )
    assert result["created"], result
    rows = (await session.execute(select(SimPosition))).scalars().all()
    assert len(rows) == 1
    position = rows[0]
    assert position.status == "open"
    assert position.order_type == "market"
    assert position.extra["auto_paper"] is True
    assert position.extra["ai_direct_paper"]["run_id"] == 9001
    assert position.leverage <= 3
    assert position.ai_setup_id is None

    duplicate = await direct.execute_direct_paper(
        session, run_id=9001, proposal=signal,
        reference_price=1000., current_price=1000.
    )
    assert not duplicate["created"] and duplicate["reason"] == "run_already_executed"

    next_run = await direct.execute_direct_paper(
        session, run_id=9002, proposal=signal,
        reference_price=1000., current_price=1000.
    )
    assert not next_run["created"] and next_run["reason"] == "verified_one_position_limit"


@pytest.mark.asyncio
async def test_ai_direct_respects_pause_and_strategy_disable(session, monkeypatch):
    monkeypatch.setenv("AI_DIRECT_PAPER_ENTRY_ENABLED", "1")
    monkeypatch.setenv("MULTI_SETUP_ANALYST_ENABLED", "1")
    settings = await get_or_create_settings(session)
    settings.auto_open_paper_positions = True
    settings.initial_balance_set = True
    runtime = await get_runtime_settings(session)
    runtime.paper_trading_paused = True
    result = await direct.execute_direct_paper(
        session, run_id=99,
        proposal={"decision": "LONG", "stop_loss": 980, "take_profit": 1100},
        reference_price=1000, current_price=1000
    )
    assert not result["created"]
    assert result["reason"] == "paper_not_enabled_or_paused"
