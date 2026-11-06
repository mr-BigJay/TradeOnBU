# Candidate diagnostics

This change instruments `verified_reversal_v1`; it does not relax entry gates, change prices or alter the strategy version. Counts describe branch events across scanned candles, **not distinct opportunities**, and cannot be interpreted as lost profits.

Each new verification run records `candidate_audit` in `structured.verified_plan_review`, and in `validation` when no candidate survives. It includes the evaluation timestamp, price, input counts, hourly regime and reference pivots, separate LONG/SHORT stages/reasons, and up to three timestamp/OHLC examples per reason per direction. `excluded_before_pattern_search` explicitly identifies the hourly direction veto; it does not mean a valid setup was discovered and rejected. Invalid input is labeled `data_blocked`.

Previously silent paths now identify missing reference swings, no sweep, sweep without reclaim, already-swept liquidity, already-broken structure, invalidation before break, no close through the structural reference and an exhausted break window. Existing displacement/FVG/freshness/SL/reward gates retain their previous behavior.

## Current read-only probe

```
docker compose exec -T tradeonbu python -m app.verified_probe
```

## The reported historical interval

After deploying this change:

```
docker compose exec -T tradeonbu python -m app.verified_audit --first-run 765 --last-run 776
```

The command reads SQLite in `mode=ro` and makes public candle requests only. No AI calls, messages or orders. Prefer stored audits when present. For older runs it reconstructs native timeframes up to each run's completion, excludes future/forming bars, and requires contiguous fresh history in all five timeframes. Missing data produces `data_blocked`, not a false absence of candidates. Requests are bounded to 301 run IDs spanning at most a day and 32 pages per timeframe.

Historical completion time approximates the original scan time; latest closed 1m close approximates the missing live tick. Current candle limits and current stored Paper costs are used, and the exchange may revise historical candles. This is diagnostic reconstruction, not exact execution replay or a profitability backtest. Do not change thresholds solely from its candidate count.

## Existing archive check

Re-running the previously supplied 26,668-minute archive with the instrumented component replay produced zero candidates, unchanged from the baseline. The source had no missing minutes or invalid OHLC under the existing carried-open policy (1,219 carried-open cases). Diagnostics include absence of sweeps/reclaims/breaks as well as downstream displacement, freshness and net reward rejections; the hourly direction exclusion is not the sole gate. The old archive ends before November 6 and cannot explain runs 765–776 directly. Repeated branch counts are not unique setups. No AI approvals or portfolio performance were simulated.
