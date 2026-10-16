# Paper Execution Engine — funnel audit & design

## Problem (observed funnel)

Most AI hourly runs produce **WAIT/READY scenarios**; a smaller fraction becomes **pending limit orders**, and only a subset **fills** into open paper positions. Primary blockers (from lifecycle + miss-audit buckets):

| Stage | Typical failure modes |
|--------|------------------------|
| AI → WAIT | Forced new setups each hour; duplicate POI bands; NO_TRADE under-used |
| WAIT → READY | Missing closed 5m confirmations (sweep / MSS / POI sequence) |
| READY → order | Auto paper disabled, wallet unset, net R:R &lt; 2 after costs |
| Pending → fill | **Always limit** while price stays away from POI; setup expiry; structural invalidation |
| Concurrent caps | Multiple simultaneous pending limits (legacy `max_simultaneous_positions=3`) |

## Engine changes (this PR)

1. **Entry mode** — `paper_entry_mode.choose_paper_entry_mode`: market paper when confirmed and live price is at/near POI; limit for retest; **no chase** beyond `LIMIT_ENTRY_MAX_DISTANCE_BPS`.
2. **Budget** — `paper_execution_budget`: max **1** concurrent auto paper entry; max **2** auto entry **orders per Tehran calendar day** (DB-backed, restart-safe).
3. **Net R:R at execution price** — market entries re-check R:R using live price with taker slippage model.
4. **AI instructions** — explicit NO_TRADE acceptance; continue_setup_id; anti-duplicate hourly setups.
5. **Metrics API** — `/api/gui/opportunities/execution-metrics` and summary `execution_kpi` (fills, candidate→fill, win rate — not WAIT counts).
6. **Entry policy compare** — `/api/gui/opportunities/entry-policy-compare` (not a profit backtest) replays stored `live_price_at_ready` promotion snapshots; `/execution-backtest` is a deprecated alias.

## How to read backtest output

- Does **not** invent fill probabilities.
- Compares **legacy always-limit attempts** vs **new policy** (skip / market / limit) using stored `live_price_at_ready` when present.
- **`uncertainty: true`** when &lt; 20 READY samples or &lt; 5 observed fills — treat rates as indicative only.

## E2E / safety preserved

- Paper-only; `auto_open_paper_positions` gate unchanged.
- ICT objective confirmations before READY promotion unchanged.
- `HARD_MIN_AUTO_PAPER_NET_RR = 2.0` unchanged.
- Market data freshness gates in monitor unchanged.

## Dashboard

Opportunities v2 shows **Paper execution (30d)** KPI band above monitoring WAIT/READY cards.
