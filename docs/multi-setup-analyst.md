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

## Conditions and execution

| Family | Forward conditions on separate closed 5m candles |
| --- | --- |
| Reversal | Liquidity sweep, then MSS |
| Continuation | Zone pullback, then directional close beyond previous candle high/low |
| Breakout/retest | Close crosses fixed level, then later retest holds the breakout side |

Continuation additionally requires aligned 1H swing structure and no opposed 4H swing
structure. All families wait for a later limit fill after these conditions. Earlier
historical events are not seeded as confirmation. The existing balanced order mode
allows the resting limit before a subsequent entry touch, but these new contracts
use closed 5m confirmation, not partial 1m substitutes. 1m data still checks freshness,
missed entry and fills. Plans expire four hours after the observed input timestamp.
An invalidated or already consumed plan is never backfilled as an executed trade.

All accepted families persist independently in Opportunities. The existing paper
risk guard remains: 0.25% equity risk budget, maximum 3x, one pending/open position,
1% daily loss stop, fee/slippage-aware RR, immutable contract binding and duplicate
order checks. Existing open positions retain their lifecycle. No live Bitunix orders.

## Schedule, reporting and cost

Scheduled analyses run at 00:01, 04:01, 08:01, 12:01, 16:01, 20:01 UTC after candle
close. The first startup/manual review can occur during the current slot. A durable
journal before network I/O limits trading analysis to one paid attempt per UTC 4H slot,
including failed attempts and manual clicks: at most six per UTC day. Failed calls do
not automatically incur another paid attempt in that slot. Daily Outlook remains a
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
