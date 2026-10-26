# Shared AI entry strategies

A (`ict_reversal`) preserves existing ICT behavior and profiles.
B (`trend_continuation`) selects an aligned 4H/1H trend pullback. Its objective
sequence is entry-zone pullback, directional close beyond the previous execution
candle high/low, then entry-zone touch.
C (`breakout_retest`) supplies a fixed `breakout_level` inside the entry zone.
Its sequence is a close crossing that level, a later candle touching the level and
closing on the breakout side, then entry-zone touch. A wick-only break is insufficient.
B/C do not require reversal sweeps or FVGs. Both persist as conditional WAIT.
Confirmation starts after monitoring begins; earlier breakouts do not count.
STRICT uses closed 5m bars. BALANCED uses actual closed 1m bars for new tokens,
not accumulated partial-5m wicks. Existing chronology/integrity checks remain.

All three default to enabled. AI analysis page switches use admin-only PUT
`/api/gui/chatgpt/strategies/settings`; GET `/api/gui/chatgpt/strategies` reads them.
At least one stays enabled. Existing SettingEntry storage and audit log are used;
no migration. Disabling B/C restores the exact old A prompt. Legacy snapshots
without strategy_id retain A semantics.

One shared inference evaluates enabled strategies. Top-level fields retain one
primary plan for the report/chart. `strategy_candidates` contains a complete plan
or explicit null for each other enabled strategy. Up to one candidate per strategy
per run is persisted independently. Missing candidates are recorded as missing,
not silently interpreted as a completed evaluation. Each plan passes the existing
strategy, contract, expiry, RR and enabled-setting validators.

Duplicate/revision checks and the same-direction *setup* guard are scoped to
strategy. Legacy unlabeled plans retain A semantics. Cross-strategy revisions
remain forbidden. The same-direction *position* guard, global position capacity,
loss/reconfirmation checks, wallet constraints and fill checks stay shared.
Monitoring several opportunities does not authorize several simultaneous trades.
There is no new optimal-position ranking algorithm; existing monitor order and
execution constraints govern admission when multiple candidates are ready.
No extra AI calls, schedule/model/reasoning changes. Actual token usage and the
switch snapshot are retained per run and visible in detail, not a cost guarantee.
Daily outlook is unchanged. AI judges higher-timeframe trend and price structure;
schema checks cannot independently prove those judgments. Backend checks alignment
for B, finite ordered levels, structural stop/invalidation and gross R:R >= 2 at
the worst entry-zone edge. Existing net R:R, fees, position/loss/duplicate and fill
safeguards still apply. Setup and auto paper position snapshots retain strategy ID.
A setup cannot change strategy through revision. Changing C's breakout level resets
confirmations even for small price changes.

Switches are checked at candidate persistence, order creation and AI-linked paper
fill. Disabling blocks new entries; pending orders cancel when their fill is next
evaluated. Monitoring can continue; enabling can resume an unexpired WAIT setup.
Open positions continue being managed. An in-flight report can show a candidate
whose strategy was disabled during inference; persistence rejects that candidate.

Validation uses mocked AI and offline candles. Profitability and live-provider
output quality are not established. Compare actual usage and paper results after
deploy. No paid inference, production deployment or real order was performed.

Regression note: `test_record_run_outcome_sets_future_next_attempt` also fails on
the unchanged base: it expects completion-relative +59 minutes, while the shipped
scheduler uses fixed UTC boundaries plus one minute. This change does not edit the
scheduler or that test.


## Diagnostics and local quality evidence

`_artifacts.strategy_persistence` records per-strategy creation, revision, rejection,
setup ID and lifecycle at report time. The panel displays these historical results;
the opportunities page remains the source for the current lifecycle and rejection.
`setup_persistence` and `opportunity` retain the primary-plan compatibility fields.

`strategy_features` is computed from closed 4h/1h/5m candles: Wilder ATR14/ADX14,
EMA20 and one-bar change, candle body and EMA distance in ATR units, relative
volume versus the preceding 20 bars (excluding the current bar), and prior-20-bar
extremes. Insufficient, gapped or invalid history is unavailable. These are soft
context for the model, not fixed score weights or new mandatory entry thresholds.
No additional market-data or LLM requests are made; extra input and candidate
output tokens can increase cost. Existing model, cadence and report count remain.

`_artifacts.strategy_timing_review` retrospectively replays B/C on the last hour
of closed 5m bars. It is explicitly hindsight-only: current levels were selected
AFTER those candles. It is neither a controlled backtest nor proof of a missed
trade. It never seeds confirmation state, places orders, or changes monitor start.
A future live change accepting earlier evidence requires an out-of-sample replay
with levels established before the evaluated bars. Strict/Balanced behavior and
fresh-confirmation rules have deliberately not been relaxed.

Read-only production funnel and paper-result cohorts (from repository root):

```bash
docker compose exec -T tradeonbu python - --days 7 < scripts/strategy_audit.py
```

This reports creation/revision/rejection counts, current setup states and reasons,
paper order states and realized net PnL by strategy. Older unlabeled rows remain
`legacy_unspecified` in performance reporting rather than falsely attributing them.
Compare cohorts with enough closed trades and different market conditions; these
counts do not establish improved win rate, drawdown, or profitability. No historical
production dataset is bundled. The new candidate contract should also be checked
against the first real inference after deployment; unit tests use mocked responses.
