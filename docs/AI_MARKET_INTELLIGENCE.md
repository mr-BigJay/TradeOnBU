# Read-only public-exchange context for AI ICT analysis

- AI analysis still runs once per configured analysis schedule (or a manual trigger). This change **does not create extra ChatGPT calls**.
- At inference time, load the latest `market_data_snapshots` record for the configured symbol. These snapshots are collected independently every 15 minutes by PR #33 and do **not** invoke OpenAI.
- Pass compact `market_intelligence` JSON beside the existing OHLCV `market` and `memory`. No historical GPT forecast prices are used.
- Fail closed (omit metrics) if snapshot age exceeds 25 minutes, symbol mismatches, quality is unavailable, or numeric values are invalid. Book imbalance is omitted after 3 minutes. The 5-minute Bitunix side delta is **provisional, not verified CVD**. Binance OI is explicitly labeled Binance USD-M, not Bitunix.
- Any included liquidation data consists of **observed historical liquidation events**, labeled per exchange with partial coverage and quality. No future heatmap or guaranteed price levels are inferred.
- Context is capped at 2600 JSON characters; evidence is observational only and cannot bypass four ICT confirmation gates, R:R or paper-only restrictions.
- If public data is missing (including disabled Liquidation Intelligence), analysis continues with its original candle and setup-memory input, with `market_intelligence.status=unavailable`.
- The retired price forecast, its UI, routes and execution guard remain untouched.

## Validation

Inspect the next AI analysis input/log in a safe environment for optional `market_intelligence` availability. Check `market_data_snapshots` collection is actually healthy, not merely the backend process. Compare forward **paper-only** outcomes and hallucinations against the existing baseline; this change has no proven positive effect on profitability.
