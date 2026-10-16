# Forward Paper E2E Validation

Isolated SQLite database: `backend/data/test_forward_paper_e2e.db` (never production/staging).

## Flow under test

`AI conditional setup → closed 5m ICT confirmations → READY → paper limit → closed 1m fill → POSITION_OPEN`

Uses real `OpportunityMonitorService`, `ict_confirmation_engine` (via monitor), and `SimPositionMonitorService`. Only market klines and wall-clock are scripted.

## Run

```bash
cd backend && python3 -m pytest tests/forward_e2e/ -v
```

## Scenarios

| ID | Expectation |
|----|-------------|
| success_long / success_short | Full pipeline + single order + fill |
| open_5m_candle_does_not_promote | Forming 5m bar must not complete sequence |
| incomplete_confirmation | WAIT, no order |
| net_rr_below_min | No paper order |
| stale_market_data | No promotion |
| incomplete_5m_bundle | Gap suspend, cursor unchanged |
| structural_invalidation | INVALIDATED |
| limit_order_never_fills | pending_entry retained |
| monitor_restart | No duplicate orders |

Each passing test builds a `ScenarioTimeline` report (events + decision reason) in assertions.
