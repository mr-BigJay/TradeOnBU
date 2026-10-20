# Price Forecast retirement (public data preserved)

The BTC **Price Forecast** tab is removed from desktop/mobile navigation.
The paid ChatGPT six-hour forecast feature is intentionally retired:

- No `price_forecast_scheduled` APScheduler job.
- Every startup persists `price_forecast_settings.enabled=false` and `next_run_at=NULL`.
- Legacy PUT `/api/gui/forecast/settings` and POST `/api/gui/forecast/generate` return HTTP **410**.
- `run_price_forecast` fails closed in production even when called internally.
- Historical forecast runs, points, snapshots and usage statistics are **not deleted**. Read-only status/chart endpoints remain for compatibility.

Public market data **continues** without ChatGPT or a paid LLM:

- `BitunixMicroDataCollector` retains its trade WebSocket rollups and depth observations.
- A separate `public_market_snapshot` APScheduler job stores snapshots every 15 minutes using the existing collector: OHLCV, Bitunix funding, order-book summary, historical trade-flow rollups, Binance public open interest, and any enabled real-liquidation intelligence.
- The optional Bybit/Binance liquidation collector is unchanged and remains governed by `LIQUIDATION_INTELLIGENCE_ENABLED`. This PR does **not** automatically enable it.
- Public exchange data requests still use exchange APIs, but **not** the ChatGPT/OpenAI paid inference API.
- No changes to ICT AI Analysis, STRICT/BALANCED paper entries, risk limits or live-trading safeguards.

## Operations
After merge, deploy by backing up SQLite and rebuilding the backend. Verify `price_forecast_settings.enabled=0`, `next_run_at IS NULL`, a healthy trade WebSocket collector, and recent rows in `market_data_snapshots` after 15+ minutes. Existing market micro-rollups and observed liquidation events are preserved.

**Storage notice:** This retirement does not delete previously stored data. The new public snapshots will continue growing the SQLite database; review a data-retention policy separately if needed.
