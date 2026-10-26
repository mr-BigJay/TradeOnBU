# BehDark → simulated positions

This optional integration reads fresh text from the existing server Telegram Web reader and creates **SimPosition** orders. It never invokes a live exchange execution adapter or an LLM. Enable explicitly with `TELEGRAM_PAPER_ENABLED=1` in the application and `PAPER_EXPORT=1` in the reader.

## Supported first version

Exact six-line Buy/Long or Sell/Short template: symbol ending USDT, entry range, one/two targets, Stop, leverage range, Balance percentage. Decimal prices, hyphens/underscores/en/em dashes are accepted. The first target is used; subsequent targets are retained as metadata. Other messages, image-only signals, replies and ambiguous text are not instructions. Channel title and numeric ID must match. No private content is embedded in the test suite.

Only non-baseline posts received within 120 seconds of their Telegram timestamp can be exported. The initial scan after startup/reconnect never trades. The backend waits up to 15 minutes after receipt for price to be inside the entry range; a fresh reader heartbeat is required. Both quote and actual fill must stay in range, including simulated slippage. Signal edits detected before entry cancel eligibility. Open positions retain their original SL/TP; **follow-up close/cancel/move-stop prose is not applied automatically**, nor are deletions tracked. Existing paper monitoring handles SL/TP, with candles fetched separately for each symbol.

Margin is the smallest of the panel default margin, maximum margin, channel Balance percentage of paper wallet balance, and available balance. Leverage is the lower of the channel's lower bound and panel default. Paper pause, total active-position limit, one active position per symbol, wallet reservation and existing risk checks apply. A minimum net R:R of max(2, runtime minimum) is checked before and after filling, including fees. The existing 3R take-profit cap still applies. These are execution policies, not a promise of profitability.

Every processed message has a durable primary key in `telegram_paper_events`; its order and reservation commit in the same database transaction. Duplicate/restarted delivery does not open another order. A failed/range-invalid fill rolls back its reservation and position. Never delete this journal to retry a historical signal. Unsupported exchange symbols, missing/mismatched ticker symbols, nonpositive/nonfinite prices and stale exchange timestamps fail closed. A missing exchange timestamp is accepted only from a newly fetched response, with no cached fallback.

## Data boundaries

Reader account profile stays in its existing private `reader_state` volume. A separate `tradeonbu_telegram_inbox` volume carries only bounded message events and a heartbeat. It is read-only in the application; no browser profile is mounted there. Reader event outbox retains one day, message storage seven days/500 rows. Database event journal retains keys for deduplication. Root-owned app can read reader-created 0600 files; do not change the application container user without configuring matching read permissions.

DOM-only collection can miss posts during outages or Telegram UI changes. It is not a guaranteed complete feed. No API key cost is introduced, but Bitunix public requests and server resource usage increase. Only paid model calls are avoided; not all operational costs.

## Deploy

`scripts/deploy_telegram_paper.sh COMMIT_SHA`, run as root, checks a clean main branch and fast-forward ancestry, backs up the application env/database, installs the pinned code, rebuilds the separate reader, and enables the app with a managed Compose override. It does not overwrite an unrelated existing override. It preserves Telegram profile volume, and the reader's first scan is a baseline. Docker runtime/deployment needs to be exercised on the VPS; this development environment has no Docker daemon.

The Positions page shows a BehDark status panel with last event reasons, and a BehDark label on its active/history positions. Waiting entries exist in this panel/journal, not as reserved pending orders. Wallet/risk rejects are terminal; market outages, position-count/symbol conflicts and range misses wait until the 15-minute expiry. A persistent unsupported symbol therefore ends as expired without opening.

To stop NEW entries (existing positions continue normal paper SL/TP monitoring):

```sh
cd /opt/tradeonbu
TELEGRAM_PAPER_ENABLED=0 docker compose up -d --no-deps tradeonbu
```

For permanent disable, set `TELEGRAM_PAPER_ENABLED=0` in `.env` as well. Set `PAPER_EXPORT=0` in `/opt/tradeonbu-telegram-reader/.env` and run Compose there to stop exporting. Do not remove volumes or restore old databases while positions are active. Re-enabling may resume fresh waiting events still inside their original 15-minute window.

## Verification without waiting for a real post

Backend tests simulate fresh long/short signals, history/edits/unsupported messages, duplicate file delivery across independent ticks, quote changes midway through creation, wallet rollback, paper pause, symbol-specific monitoring and ticker identity validation. No synthetic message is injected into the production account or wallet. Reader tests cover fresh-only export and baseline/edit behavior; UI/build tests cover the existing Positions page. Real continuous delivery of a newly published channel post remains an observational check after deployment, not a prerequisite for starting this paper-only implementation.
