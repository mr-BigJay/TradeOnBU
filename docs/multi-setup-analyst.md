# Multi-scenario paper analyst

`MULTI_SETUP_ANALYST_ENABLED=1` (Docker Compose default) enables `multi_setup_analyst_v1`.
The API receives closed 1m/5m/15m/1H/4H market data, candle-derived support/resistance,
order blocks and FVG catalog, and the existing freshness-filtered market intelligence.
Missing optional context is explicitly not a direction signal. The old reversal candidate
filter no longer controls whether an analysis request is made.

One response compares reversal, continuation and breakout/retest, and selects zero to
three conditional plans, at most one per family. No requirement to force a signal.
The model selects catalog IDs and explains its choice; it cannot directly provide
executable prices or claim confirmations. The server computes midpoint limit entry,
structural invalidation plus ATR-buffered stop, and the nearest tick satisfying net
1.4R before the closest applicable 15m/1H/4H obstacle. If this target does not fit,
the plan is rejected. The selected target level is a ceiling, not a promised destination.
The stop is at least one 5m ATR away and cannot exceed three 15m ATRs.

## AI-first direct Paper market entry (explicit opt-in)

Set `AI_DIRECT_PAPER_ENTRY_ENABLED=1` in the deployment's `.env` and recreate
the `tradeonbu` container to enable this independent Paper-only mode.
The default is off, so an existing deployment cannot silently start trading.
This uses the **same paid model call** as the four-hour analyst; no new call
is added. The model sees the same closed 4H/1H/15m/5m candles, actual
market context, price levels, indicators and analyst evidence. The payload also
supplies `direct_paper_execution`: current reference price, actual Paper
taker/maker fee rates, assumed slippage, a 2.0 net-R requirement, and the
2.5 gross-R execution target cap. In direct mode, the response **must**
include a separate `trade_now` object with a directional view, **numeric**
stop, target and Persian thesis; it can still return `NO_TRADE` when
there is no plausible structural stop/target. Empty conditional `plans`
or a ranging 4H label alone must not veto the independent decision.
No extra model calls are made.

If the model chooses a coherent market entry, the backend fetches a fresh
Bitunix ticker and enters a **market Paper position immediately**, not a
conditional limit or a multi-bar Sweep/MSS/retest monitor. The exit SL/TP
are always attached and are checked with market taker fees and slippage
to achieve >=2.0 net R before a capped executable TP. Never force a bad
LONG/SHORT, and never chase if the price moved more than 0.50% since the
AI snapshot. Market entry is only allowed when the paper wallet is initialized,
auto Paper is enabled and not paused or dry-run, the A strategy is enabled,
and one active Paper position does not already exist. Verified risk controls
are reused: 0.25% equity at risk per entry, 1% daily realized loss cutoff,
max 3x and fee-aware risk-size reduction. It never places live Bitunix orders.

Every attempted direct trade logs the decision and outcome in the four-hour
analysis and Telegram, and stored positions include the originating analysis
run ID. The original ICT confirmation monitor remains unchanged for *other*
conditional candidate strategies. The model may still return a conditional
watchlist, so a direct AI position and other forecasts are distinct.
Avoid conflating "AI confidence" with validated probability or live success;
measure forward Paper outcomes before enabling real trading elsewhere.

## Major structural levels for four-hour forecasts (watch-only)

The **outlook** now uses a separate, server-validated `outlook_major_levels` shortlist. The
executable plan's complete 5m/15m/1h/4h evidence catalog is unchanged. This separation
prevents low-timeframe pivots being promoted to the main four-hour support/resistance report.

Selection requires a real confirmed 1h/4h swing from closed Bitunix bars or an
uninvalidated HTF displacement order block, a bounded zone width, no later
closed-candle break of the zone, and an ATR-scaled move away. A normal 1h pivot
also needs an independent corroborating HTF level or an unusually large 4h-scale
reaction. Nearby same-side levels are deduplicated, preferring stronger 4h
evidence. The selected output preserves original catalog IDs and price ranges;
no intermediate, invented or widened support/resistance is created.

For each four-hour watch scenario, **both** source and destination (if given)
must belong to the independent major-level shortlist. Targets that overlap the
source, do not leave sufficient **full-zone** room (at least max(1h ATR,
0.45×4h ATR)), or skip a nearer major barrier are rejected. Inadequate evidence
means fewer scenarios or zero; never fill four just to make the report look busy.
The Telegram report and the analysis history also list corroborated major levels,
even when no watch scenario is selected.

These thresholds are deterministic engineering heuristics, **not scientifically
calibrated support strength or profitability estimates**. Review live false
positives/negatives against subsequent closed candles before tuning. No 1D
history is currently used to prove multi-week levels. 5m/15m remain exclusively
for execution timing and are not upgraded to main outlook zones.
All Paper order filters, stops, fees, risk limits and confirmation gates are unchanged.

## Conditions and execution

| Family | Conditions on separate closed 5m candles |
| --- | --- |
| Reversal | Sweep/reclaim of selected invalidation reference, then close crossing the fixed confirmed opposite 5m pivot |
| Continuation | Zone pullback, then directional close beyond previous candle high/low |
| Breakout/retest | Close crosses fixed level, then later retest holds the breakout side |

Continuation additionally requires aligned 1H swing structure and no opposed 4H swing
structure. All families wait for a later limit fill after these conditions. A complete
sequence in the last 15 minutes can be seeded from closed candles, only when its
reference areas were already known at the event time and no later entry was consumed.
No historical fills are created. Incomplete sequences remain forward-only. The existing balanced order mode
allows the resting limit before a subsequent entry touch, but these new contracts
use closed 5m confirmation, not partial 1m substitutes. 1m data still checks freshness,
missed entry and fills. Plans expire four hours after the observed input timestamp.
An invalidated or already consumed plan is never backfilled as an executed trade.

All accepted families persist independently in Opportunities. API ranking is retained
and used by the monitor before the single-position budget is consumed. Existing plans
without ranks sort after ranked plans; ties prefer the newer analysis.
At order time the stop is widened when needed to protect the observed sweep/pullback/
retest extreme with an ATR buffer. The target and monetary risk size are recalculated;
the target must fit before the original structural obstacle and the stop must remain
within three 15m ATRs. Otherwise a specific execution rejection is recorded. The signed
proposal remains immutable; final prices and their confirmation evidence are stored
separately in the position's `execution_plan`.
The existing paper
risk guard remains: 0.25% equity risk budget, maximum 3x, one pending/open position,
1% daily loss stop, fee/slippage-aware RR, immutable contract binding and duplicate
order checks. Existing open positions retain their lifecycle. No live Bitunix orders.

## Schedule, reporting and cost

Scheduled analyses run at 00:01, 04:01, 08:01, 12:01, 16:01, 20:01 UTC after candle
close. The first startup/manual review can occur during the current slot. A durable
journal before network I/O limits trading analysis to one normal attempt per UTC 4H slot,
including manual clicks. One additional attempt is allowed only after a transient
transport/429/5xx error, at least two minutes later, and only if recorded output use
was below 5,000 tokens. Authentication, quota, malformed JSON and output exhaustion do
not qualify. A crash with unknown outcome cannot reset the budget. Normal usage remains
six trading calls/day; a day with retryable failures can reach twelve. Provider usage
is saved before JSON validation and from partial error responses when available.
Daily Outlook remains a
separate daily request and cost; reducing Telegram messages alone does not save API cost.
The old duplicate scheduled 4H Outlook job is disabled in this mode. Analysis shows the
new API report; historical 4H Outlook reports remain accessible in a collapsed archive.
Existing Telegram delivery sends the analysis with its chart when available. Full
multi-plan comparison is stored in the analysis report. No extra inference per strategy.

The settings namespace `analyst_strategies` starts with all three families enabled;
old `entry_strategies` preferences remain available for legacy-mode rollback. Strategy
switches edit the active namespace. Setting the new flag to 0 restores the old reviewer
path (and its cadence); this is a mode change, not deletion of existing orders.

## Audit and validation

Each analysis stores the catalog, selected source references, proposal/accept counts,
rejection reasons, comparison, response usage and UTC slot. The deployment probe shows
`evidence_count`, which counts potential source areas, NOT signals or positions.

`backend/tests/test_multi_setup_analyst.py` covers malformed/invented references,
contract changes, structural trend/target rejection, real three-plan persistence,
one-call budget, and real opportunity/position monitor paths for all three families:
wait -> condition -> pending limit -> subsequent paper fill. External inference and
market data are synthetic fixtures, not profitability evidence. Existing backend and
frontend regressions must pass as well. Historical v3 reversal replay statistics do
not measure this AI selector; evaluate its separately versioned forward paper results.

## Descriptive indicator context (version 2)

Trading analysis receives EMA9/EMA20, their one-candle changes, last-candle cross and
relative ordering, close-to-EMA distance in ATR units, RSI14 with one-candle change
and five recent timestamped readings. Existing ATR14/ADX14/relative volume remain.
Coverage: 5m, 15m, 1H, 4H. Daily Outlook receives 4H and 1D from its existing candle
snapshot; it makes no extra exchange or model calls for indicators. The dashboard
shows the exact saved input in each new report's indicator table, not live values.
Old reports without version 2 show an explicit missing-data message.

EMA reuses the existing SMA seed and alpha=2/(n+1). RSI uses Wilder smoothing seeded
by the mean of the first 14 close changes; positive-only/negative-only series return
100/0. With both averages zero, RSI is undefined and recorded as null. Missing or
non-contiguous history remains unavailable, never zero-filled. Calculation conventions
and source history count are included in the API context. Identical readings to a
TradingView chart are not promised: market feed and available warm-up history differ.
Definitions: https://www.tradingview.com/support/solutions/43000502338-relative-strength-index-rsi/
and https://www.tradingview.com/support/solutions/43000592270-exponential-moving-average/

These metrics are model context only. No RSI threshold, EMA cross requirement,
confirmation sequence, risk rule, order rule or scheduler change is introduced.
They add a small input payload to existing requests, not an additional paid request.
