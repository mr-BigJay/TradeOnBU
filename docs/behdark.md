# Independent BehDark paper workspace

## Isolation

This replaces the **proposed**, unmerged shared-wallet integration. Do not deploy
`scripts/deploy_telegram_paper.sh` from the older reader branch. This branch starts
from main and changes no BTC/ICT analysis, scheduler, position service, wallet,
market collector, or production model table.

- `backend/behdark` is a standalone FastAPI process/container with its own SQLite
  volume (`tradeonbu_behdark_data`). It cannot import `app.db`, `app.models`, or
  execution engines; it has no main DB mount, exchange key or OpenAI key.
- Reader retains its existing browser profile and CPU/RAM budget. It writes a
  bounded inbox volume; only the BehDark service mounts that inbox read-only.
- Main dashboard adds a menu and an authenticated, timeout-bounded HTTP proxy.
  Mutations require the existing admin/CSRF authorization. No background BehDark
  job runs in the main process. There is no dependency from the main service to
  BehDark; disabling BehDark leaves the main application available.
- The sidecar is capped at 0.25 CPU, 256 MiB, 64 PIDs; reader caps are independent.
  Shared physical host resources are not total hardware isolation.

## First use and deployment

Fetch the reviewed release, then run `bash scripts/deploy_behdark.sh <sha>` on the
server. The script fast-forwards only a clean tracked main checkout, backs up
main DB/environment and any existing BehDark DB, refuses unrelated overrides,
preserves the reader profile and Power Saving marker, and maintains its current
0.75 CPU/1 GiB limits. It rebuilds the dashboard once to add the page/proxy; this
requires a brief main container replacement. It never merges on GitHub or changes
trading settings. On install failure inspect the printed output and backup;
there is no automatic destructive rollback or reset. Caddy is not recreated.

Open **BehDark → Wallet**, set initial virtual USDT once, then choose margin,
leverage, assumed per-side fee in basis points, optional signal lifetime and
entry-enabled state. No signal becomes executable before the persistent first
activation timestamp. Turning entries off keeps exits and signal ingestion
running; turning them on does not reset the cutoff. Initial capital cannot be
reset over an existing ledger. New entry settings are frozen into each fill.

To stop this entire independent service: `docker compose stop behdark` (open
paper trades will also stop being monitored). To pause just new entries, use the
wallet checkbox. To remove the menu without deploying code, no separate toggle
is provided. Restore app code/env using the recorded backup only after reviewing
any later application migrations; never replace the main DB casually.

## Data and execution policy

- New visible BehDark messages are keyed by channel/message ID, archived once and
  revisions preserved. Text parsing supports the explicit Buy/Long or Sell/Short,
  entry-range, targets, stop, leverage and balance template. Unsupported/image
  messages and replies become review records; there is no OCR, generative parsing,
  or automatic interpretation of prose cancellation replies/deletions.
- An edit to a pending signal stops automatic entry and requests review. An edit
  to an open trade is recorded without silently moving its original stop/targets;
  results are flagged uncertain. Manual cancel is available for waiting/review
  signals. A review signal is not automatically made executable.
- Signal lifetime defaults to **no time expiry**, rather than 15 minutes. A stop
  reached before entry invalidates a waiting signal. Initial observation more
  than 120 seconds after receipt is review-only; missed history isn't backdated.
- Only symbols with waiting/open paper signals are polled. Duplicate symbol
  signals share data. One batched ticker call and bounded 1-minute candle calls
  are used; no AI API requests. Open positions have priority within the default
  20-symbol budget; overflow waiting symbols are clearly queued/uncertain.
- Poll loop sleeps 5 seconds after work. HTTP requests have 8-second timeouts and
  connection limits; exact tick-by-tick fills are **not** claimed. Most recent
  closed 200 one-minute candles are retrieved on a one-minute due schedule (at most
  two symbols per loop, oldest request first);
  data outages beyond this recovery window remain uncertain. No full-market
  history, depth or trades are collected. Quotes carry receipt time; venue ticker
  timestamps, when present, are checked. Without them freshness is receipt-based.
- Current quotes inside the entry range fill at the observed price. Historical
  candle range touches use the near edge (LONG upper, SHORT lower). No fill is
  backdated before receipt. A candle touching entry and TP/SL with unknown order
  becomes review-only. Candle-only entries and recovery after newer quotes are flagged uncertain.
- Open trades take equal fractions at each distinct target, with a fixed original
  stop (no automatic breakeven/trailing). Stop/target ambiguity uses adverse stop
  first and excludes the result from certain winrate. Gaps use adverse observed
  prices. No depth/queue model, funding or exchange liquidation emulation; loss
  is capped to the released isolated virtual margin, with an approximate margin
  exhaustion threshold. Leverage and margin come from this wallet, not A/B/C or
  channel capital percentages. Default 6 bps is a configurable **assumption**, not
  a verified account fee tier.
- Insufficient funds do not erase the signal: it remains waiting with a reason.
  Reports measure executed paper-account performance, not a capital-unlimited
  shadow backtest of every channel recommendation. Unentered/unsupported/review
  records remain separately visible/exportable rather than treated as losses.
- Balances, reservations, partial fills, fees and completion persist atomically
  in the independent ledger. FIFO ordering allocates scarce wallet funds.

## Reports and accuracy

Winrate = positive-net **certain closed** trades / all certain closed trades
(including breakeven in the denominator). Pending, cancelled, review and uncertain
closed trades are excluded and counted separately. Net and fee totals, realized
closed-trade drawdown, target touches, per-symbol aggregates and CSV are provided.
This drawdown is not intratrade mark-to-market drawdown. Latest 200 finalized
records are shown, active records remain visible, and CSV covers all history.

The reader is browser-DOM based (`rendered_messages_only`). Reconnection baselines
are deliberately not traded, and unrendered/missed channel posts cannot be
promised. Real new-post receipt still needs a production observation; offline
synthetic tests do not prove Telegram delivery. Heartbeat, data problems and
queued coverage are visible. All orders are virtual; no exchange order endpoint
is present.

Public market API contracts:
- https://www.bitunix.com/api-docs/futures/market/get_tickers.html
- https://www.bitunix.com/api-docs/futures/market/get_kline

## Validation

From backend: `python -m pytest behdark/tests tests/test_behdark_proxy.py -q`.
From frontend: `npm test -- --run src/BehDarkPage.test.tsx`, `npm run build`,
`npx tsc --noEmit`. Reader tests: `python -m unittest discover -s
integrations/telegram-web-reader -p test_reader.py` from repo root. Docker build and
actual server behavior must be checked on deployment; no local Docker runtime is
assumed.
