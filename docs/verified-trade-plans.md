# Verified trade plans — paper release v1

## Problem and chosen design

The old analyst could invent entry/stop/target levels, tight-stop checks were advisory,
confirmation logic reinterpreted those plans downstream, and execution could change
TP. Hourly publication could follow the actual pullback. This release uses a single
server-created contract, then lets the API approve one candidate or veto all of them.
It is a fixed engineering baseline, **not an established profitable strategy**.

The Compose default enables `VERIFIED_PLANS_ENABLED=1`. The existing Bitunix live
adapter remains disabled. This release does not submit real exchange orders.

## Exact rules (not fitted to historical profits)

- BTCUSDT only. Check 5m/15m/1h closed OHLC, continuity, alignment and freshness.
  Before API review also check 1m/4h. Never silently fix invalid prices.
- Sweep a previously confirmed 2-left/2-right swing, close back inside; then close
  beyond the opposite confirmed swing within six bars. A pivot is usable only
  after both right-hand bars have closed. Reject prior consumption of either level.
- Break candle directional body >= previous ATR14 and >=60% of its range.
- That break is the middle candle of a real three-candle FVG. Reject zones already
  touched after formation, zones older than 30m, and quotes beyond the target.
- Entry = FVG midpoint rounded to BTC's 0.1 price grid. SL behind sweep extreme
  with 0.25 ATR14 buffer, rounded outward. Stop distance must be 1–3 ATR14.
- Reject trades opposed to 1h HH/HL or LH/LL structure; range/unclear goes to API
  review, which may veto using the full supplied market context.
- TP = first tick providing net 2R under stored Paper costs and the existing 2.5R
  gross execution cap, before the nearest unbroken 15m/1h obstacle minus 0.1 ATR.
  No room means no plan. The API cannot change any prices or create an identity.
- Candidate expires 60m after FVG formation, independent of API/revision timing.
  API latency beyond 120s invalidates its approval. Same structural opportunity
  is reviewed at most once in 24h (including rejected/failed review attempts).
- Scan every 5m at UTC boundary+60s. At most four review calls per UTC day.
  No valid candidate means zero API calls and a local diagnostic record marked
  `is_ai=false`. Old hourly interval controls are overridden in this mode.
- Existing ordered sweep/MSS proofs are seeded from server evidence. BALANCED
  infrastructure places a resting limit BEFORE the first return; it does not wait
  for a second sweep or infer a historical fill. The order-time 1m check rejects
  a zone already touched during API/scheduler latency. No market chasing.
- Shared-account one pending/open Paper position at a time. Size for 0.25% equity
  loss including entry fees, adverse SL slippage and exit fees; leverage <=3 and
  margin <= existing configured caps. Stop new orders at 1% sum of daily losses
  against estimated UTC start-of-day equity. Gap losses can exceed this modelled
  budget; it is not a guaranteed loss cap.

## What is retired from the active path

Free-form model-generated prices, B/C candidates, legacy rule scanner, automatic
paid daily outlook and structure-shadow jobs are disabled in this mode. Historical
reports, existing-position management, authentication, wallet, manual controls and
rollback code remain. Deleting their database tables would destroy useful history.
The opportunities page shows the effective policy and hides obsolete shadow controls.
Daily cards can read an actual verified pending order; they cannot invent levels.

Legacy unverified plans cannot create new automatic orders. Existing pending/open
positions continue to be managed by their original lifecycle and stops. Review old
pending orders in the dashboard when deploying; this change does not cancel them.

## Validation and known limits

`tests/test_verified_trade_plan.py` exercises actual candle detection (both sides),
corrupt/gapped/future data, consumed zones, immutable prices, API rejection/expiry,
contract persistence with exact tick/expiry, ordered pre-entry proof, risk sizing,
API budget and a real DB-backed call into the Paper order service. Synthetic test
fixtures prove software behavior, not trading performance.

Offline replay:

```bash
PYTHONPATH=backend python scripts/replay_verified_plans.py /path/replay.json.gz --output /tmp/verified-replay.json
```

The supplied archive has 26,668 one-minute rows and 1,219 out-of-range opens.
Follow-up inspection found **all 1,219 exactly match the adjacent previous close**;
none has an out-of-range close. Four cases were also re-fetched from public Bitunix.
The earlier blanket description “invalid candles” was too broad. This is observed
feed behavior, not an exchange-documented guarantee or evidence of profitability.

Compatibility accepts an out-of-range open only with an immediately preceding,
finite, positive, range-valid close that equals that open exactly. No tolerance is
used. The fetcher requests one extra predecessor and keeps evidence when trimming
windows. An unproven first-row anomaly still fails closed. Raw open/high/low/close
are unchanged; fills, stops and targets continue to use actual reported high/low.
Displacement body uses the open clipped to the reported range for that calculation
only, so an external carried price cannot inflate candle-body strength. ATR retains
its normal previous-close gap calculation. The probe separately reports carried
opens and invalid data, now including 4h. This compatibility is a Paper engineering
policy; it does not certify the feed against actual trade history.

Replay uses the same evidence rule and reports carried opens separately. On clean
input it evaluates only mechanical candidates; it does not invent historical API
approval, portfolio returns, or an out-of-sample profit claim.

## Deployment check (no trades, API calls or writes)

After building this branch in an isolated container first, run:

```bash
docker compose exec -T tradeonbu python -m app.verified_probe
```

The probe reads existing Paper costs and fetches public native Bitunix candles.
Expected: `enabled: true`, `status: data_ok`. Zero candidates is valid and diagnostic
reasons are printed. `data_blocked` requires resolving the exchange/source issue;
never disable the OHLC guard to force trades. It does not read or print credentials.
Health includes `verified_plan_version: verified_reversal_v1` and `live_trading:false`.

This session has no VPS access; neither the remote database nor deployment has been
changed. Back up SQLite with its backup API before deploying, preserve the current
image, build this exact reviewed commit, then use Compose `up --wait`. Roll back by
restoring the previous image; there is no schema migration in this release. Setting
`VERIFIED_PLANS_ENABLED=0` explicitly restores the legacy pipeline after restart;
it is not a way to validate this new strategy.

## Recorded checks for this change

Local Python 3.12 / isolated test SQLite, UTC, no inherited proxy configuration:
- Full offline backend suite: 798 passed, 2 skipped, 12 forward-E2E deselected.
- Same full suite on unchanged aa984eb source: 775 passed, 2 skipped, 12 deselected.
- Final targeted suite after the partial-5m-return fix and order-monitor integration:
  74 passed, 1 skipped. The order path goes through `_attempt_ready_paper_order`
  into the actual Paper position service, not a mocked successful order.
- Frontend production build succeeded; 60 frontend tests passed.
- Python compile, shell syntax and `git diff --check` succeeded.
- Initial strict replay blocked 1,219 out-of-range opens / 26,668; follow-up evidence
  and the compatibility policy above supersede that classification.
No production deployment, real-money execution, live market performance result,
or API-review trading win rate is represented by these software checks.
