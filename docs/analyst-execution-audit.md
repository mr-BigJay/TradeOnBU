# Analyst execution audit corrections

Applies to `multi_setup_analyst_v1` Paper contracts. No live order adapter or
additional indicator thresholds are introduced. Existing signed plans still load;
new plans carry fixed reversal references, quality metadata and recent proof.

## What changed

- Sweep/MSS are tied to the selected invalidation price and a fixed confirmed 5m
  pivot instead of interpreting an unrelated rolling-window break as that plan's MSS.
- Complete, recent closed-candle confirmations can be reused without pretending
  an order existed before registration. A later limit fill is still required; this
  is intentionally not market-on-touch or an implicit change to market orders.
- Execution SL protects observed confirmation extremes. TP and position size are
  recalculated using fees/slippage and the same 1.4 minimum net R:R. Insufficient
  space produces `execution_insufficient_reward_before_obstacle`, not a forced entry.
- Proposal rank survives persistence and controls monitor order. OB retest state,
  touch counts, formation age and FVG fill state reach the model as context.
- Partial/invalid response usage is retained; transient retry is bounded to one
  additional call per slot. Failed calls can therefore increase API costs, within
  the documented ceiling; no retry loop or per-strategy inference is added.
- Report fees now belong to the selected closed trades, rather than all wallet
  history. Version/strategy metrics use recorded, non-ambiguous Paper outcomes.

## Production observation (read-only)

```bash
cd /opt/tradeonbu
docker compose exec -T tradeonbu python -m app.analyst_audit --days 7
```

This command reports attempts, provider token use, rejected proposals, latest setup
blocking reasons and observed net results grouped by engine version and strategy.
Missing provider usage is counted explicitly; it does not mean the request was free.
Realized drawdown is measured in USDT from closed trades, not portfolio mark-to-market
drawdown. It excludes transfers and unrealized positions. The dashboard report API
also includes `performance_by_strategy` with these definitions.

`scripts/replay_verified_plans.py` remains a **legacy candle-component replay** and is
now labeled with its engine version. It is not a backtest of the current AI selector.
Historical AI choices cannot be reconstructed honestly without their recorded inputs
and outputs. No profitability or required weekly signal count is asserted here.

Analysis still runs every four hours. Opportunities forming entirely between calls
may be missed. The changes reduce redundant confirmation waiting for recent evidence;
they do not claim continuous API analysis. Old reports/proposals are not rewritten.
