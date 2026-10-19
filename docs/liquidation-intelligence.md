# Liquidation Intelligence

Independent module that ingests **real** BTCUSDT liquidation observations from public WebSocket feeds (Bybit primary, Binance secondary) and exposes a compact summary to **AI Price Forecast** only.

## Enable (default: off)

Set in environment / `.env`:

```bash
LIQUIDATION_INTELLIGENCE_ENABLED=true
```

Optional retention (days):

```bash
LIQUIDATION_RAW_RETENTION_DAYS=7
LIQUIDATION_ROLLUP_RETENTION_DAYS=30
LIQUIDATION_STALE_AFTER_SECONDS=300
```

Restart the backend container. The collector does **not** call OpenAI and does not alter ICT scheduling.

## Health

Authenticated dashboard API (same key as other GUI routes):

- `GET /api/gui/liquidation/status` — per-source connection, counters, freshness
- `GET /api/gui/liquidation/summary` — forecast-style liquidation payload preview

## Data sources & limits

| Source | Feed | Notes |
|--------|------|--------|
| Bybit | `allLiquidation.BTCUSDT` on linear public WS | ~500ms pushes; `Buy` = long liquidated; **bankruptcy price** (not guaranteed fill for entire position) |
| Binance | `btcusdt@forceOrder` | **Sampled** (~1 order / 1000ms max) — not a full liquidation tape |

**Real liquidations vs zones:** This module stores **observed events only**, not estimated future liquidation heatmaps.

## Storage

- `liquidation_events` — deduplicated normalized rows
- `liquidation_source_status` — connection health
- `liquidation_window_rollups` — optional aggregated buckets (retention job)

## Disable / rollback

1. Set `LIQUIDATION_INTELLIGENCE_ENABLED=false` and restart.
2. ICT, forecast scheduler, and GPT call count unchanged when disabled.
3. DB tables remain; safe to leave or prune manually.

## VPS live probe (read-only)

```bash
cd backend && python3 scripts/liquidation_ws_probe.py --seconds 15
```

No GPT, no config changes.

## Resource impact

- **RAM/CPU:** Two async WS tasks + periodic retention (hourly when enabled)
- **SQLite growth:** Bounded by retention settings
- **GPT cost:** Slightly larger market summary JSON when enabled (compact stats only, no raw event list)
