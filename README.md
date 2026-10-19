# TradeOnBU — ICT Trend & Signal Lifecycle (Paper)

Rule-based ICT analysis, Telegram inline approval, real Bitunix candlestick charts, and full paper-trading lifecycle. **No live Bitunix orders.**

## Hourly AI ICT analysis (ChatGPT Plus / SIWC)

TradeOnBU supports **Sign in with ChatGPT** open-source token sharing (ChatGPT Plus/Pro plan usage on the official Responses API — not a separate API key).

1. Open dashboard → **تحلیل AI** → **Connect with ChatGPT** (starts OAuth; completes on a machine with a browser).
2. On your PC (not inside Docker), copy **all** one-time values from the dashboard (`CONNECT_TOKEN`, `CONNECT_SECRET`, `OAUTH_STATE`, `AUTHORIZE_URL`). No browser session cookie is required.

```bash
export TRADEONBU_URL=https://your-domain
export CONNECT_TOKEN=...
export CONNECT_SECRET=...
export OAUTH_STATE=...
export AUTHORIZE_URL='...'
python3 scripts/siwc_oauth_helper.py
```

3. After connecting, explicitly enable **unattended hourly ChatGPT usage** in the dashboard (scheduling stays off until you consent).

For self-hosted VMs, OpenAI documents completing OAuth locally then transferring encrypted credentials over SSH — this helper posts tokens to your server over HTTPS after loopback OAuth ([SIWC self-hosted VMs](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms)).

Hourly analysis runs server-side when connected (`store: false`, `stream: true`). Failures are recorded; rule-based reports are **not** substituted as AI. **Paper only.**

## ICT opportunity lifecycle (AI-first)

Architecture (paper only):

1. **ChatGPT** proposes immediate or **conditional WAIT** plans (`setup_mode=conditional`, `final_ai_decision=WAIT`).
2. **Persistence** stores conditional plans as `AiProposedSetup` rows in `WAIT_LONG` / `WAIT_SHORT` (not dropped when `decision=NO_TRADE`).
3. **Opportunity monitor** (~60s, public Bitunix candles) evaluates objective ICT confirmations on **closed** bars — not GPT boolean flags alone.
4. **READY_*** setups pass net R:R ≥ 2 (unchanged) and may create **pending limit** paper orders when `auto_open_paper_positions` is enabled.
5. Existing **sim position monitor** fills limits and manages TP/SL.

Dashboard: **فرصت‌ها** tab → `GET /api/gui/opportunities/dashboard`.

## Liquidation Intelligence (Price Forecast input)

Optional **public WebSocket** collector (Bybit `allLiquidation` + Binance `forceOrder`) stores deduplicated BTCUSDT liquidation events and attaches a **compact stats summary** to AI Price Forecast market snapshots. **Default: disabled** — set `LIQUIDATION_INTELLIGENCE_ENABLED=true` on the VPS after testing.

Details: [docs/liquidation-intelligence.md](docs/liquidation-intelligence.md). Does not affect ICT analysis or GPT call scheduling.

**VPS verification (no deploy from agent):** after merge, pull on `/opt/tradeonbu`, rebuild containers, confirm migrations apply (new columns on `ai_proposed_setups`, `ict_setup_audit_events` table). SQLite data is preserved; do not run `docker compose down -v`.

## Phase 3 — One-command install (Ubuntu/Debian)

Production install (interactive — domain, admin username/password only). **Use the bootstrap entry point** so helper scripts are downloaded before install runs:

```bash
curl -fsSL https://raw.githubusercontent.com/mr-BigJay/TradeOnBU/cursor/ict-lifecycle-system-2ec7/scripts/bootstrap-install.sh | sudo bash
```

Alternative (same flow, explicit temp file):

```bash
curl -fsSL https://raw.githubusercontent.com/mr-BigJay/TradeOnBU/cursor/ict-lifecycle-system-2ec7/scripts/bootstrap-install.sh -o /tmp/tradeonbu-bootstrap.sh && sudo bash /tmp/tradeonbu-bootstrap.sh
```

**Not yet verified on a live Ubuntu host with real DNS/SSL in this environment** — run the bootstrap command on your server and confirm the HTTPS success banner before treating install as production-ready.

Install path: `/opt/tradeonbu` • HTTPS via **Caddy** (Let's Encrypt) • Session login (Argon2id) • Settings in dashboard (Persian RTL).

Ops scripts: `scripts/update.sh`, `backup.sh`, `restore.sh`, `uninstall.sh`  
CLI recovery: `printf '%s' 'NewSecurePass123' | docker compose exec -T tradeonbu python3 -m app.cli reset-password --username USER --password-stdin`

**Tested in dev:** 64 pytest passed, frontend build OK. **Requires Ubuntu host:** DNS, ports 80/443, real ACME certificate, end-to-end HTTPS login.

## Deployment blockers (2.2.2)

- Docker `DATABASE_URL`: `sqlite+aiosqlite:////app/data/tradeonbu.db` (host volume `./data`).
- Same 5m bar: 1m walk for entry + post-entry TP/SL; SL-first on ambiguous 1m.
- Approval: `APPROVAL_ENTRY_TOUCH_AMBIGUOUS` when created mid-1m and OHLC could hide pre-creation touch.
- `scripts/verify_deployment.sh` uses `scripts/verify_helpers.py` for JSON parsing.

## Pre-deployment reliability (2.2.1)

- Creation-boundary approval precheck via 1m candles inside the partial 5m bar at signal creation.
- `pre_data_error_status` preserved through DATA_ERROR; per-signal `fetch_historical_closed` from `last_processed_candle_ms`.
- Notification outbox marks `sent` only when Telegram returns a `message_id`.
- Single Telegram poll consumer (FastAPI lifespan loop only; worker does not poll).

## Phase 2.2 final reliability

- **Telegram approval:** Production precheck loads real 5m/1m candles from signal creation through approval; rejects expired, terminal, and late (entry already touched) setups. Approvals during a forming 5m bar use 1m granularity and set `entry_monitor_from_ms` to the next 5m open (documented in signal events / `analysis.approval_policy`).
- **Telegram polling:** `poll_once` advances the update offset only after successful `handle_update`; failed updates are retried.
- **Notifications:** Durable `notification_outbox` with dedupe keys, retries, and `sent` status only after confirmed delivery (worker flush each cycle).
- **Worker lock:** Atomic SQLite `UPDATE … WHERE lock expired OR same owner`; ms-based expiry for naive/aware safety.
- **Market data:** `receipt_time_ms` vs optional `exchange_time_ms`; gap backfill with `data_complete` gate before ICT/monitoring.
- **Verification:** `scripts/verify_deployment.sh` (Docker steps skipped when Docker is unavailable).

## Phase 2.1 reliability hotfix

- Telegram allowlist **fail-closed** (empty list denies everyone)
- Startup guard: strong `DASHBOARD_API_KEY` + `ADMIN_API_KEY` required (`SKIP_CONFIG_GUARD=1` for tests only)
- Approval endpoints require **admin** key; webhook requires 32+ char secret + `X-Telegram-Bot-Api-Secret-Token`
- Live price from Bitunix **tickers** (not stale candle close); closed vs forming candles by timeframe boundary
- Paper fills only after `telegram_approved_at`; centralized `ExecutionModel`; structural TP only
- Worker lock + Telegram offset persistence + idempotent notifications

## Phase 2 highlights

- Strict ICT validation: Sweep → MSS + Displacement → fresh unmitigated FVG
- Net R:R from actual simulated fill prices and exit notionals (fees, spread, slippage)
- Multi-candle monitor with per-signal cursor (restart-safe)
- Telegram inline buttons: Approve / Reject / View Chart (allowlisted user IDs)
- Real candlestick dashboard (1m–4h) with structure overlays
- API key auth, CORS allowlist, rate limiting — bind app to localhost; use nginx + HTTPS publicly

## Ubuntu install

```bash
sudo apt update && sudo apt install -y python3-pip nodejs npm
git clone https://github.com/mr-BigJay/TradeOnBU.git && cd TradeOnBU
cp .env.example .env   # set DASHBOARD_API_KEY, ADMIN_API_KEY, Telegram vars
pip install -r backend/requirements.txt
cd frontend && npm install && npm run build && cd ..
mkdir -p data
cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` and sign in with `DASHBOARD_API_KEY` (use `ADMIN_API_KEY` for settings/import).

### Docker (24/7)

```bash
cp .env.example .env
docker compose up -d --build
```

App listens on **127.0.0.1:8000** only. Put `deploy/nginx.conf` in front for HTTPS.

### Telegram

1. Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
2. Set `TELEGRAM_ALLOWED_USER_IDS` (comma-separated numeric IDs)
3. Long polling starts automatically unless `TELEGRAM_USE_WEBHOOK=true` with `POST /api/telegram/webhook/{TELEGRAM_WEBHOOK_SECRET}`

## Tests

```bash
cd backend
export DATABASE_URL=sqlite+aiosqlite:///./data/test_tradeonbu.db
export DASHBOARD_API_KEY=test-dashboard-key
export ADMIN_API_KEY=test-admin-key
export TELEGRAM_ALLOWED_USER_IDS=111,222
PYTHONPATH=. pytest tests -q
```

## Paper trading guarantee

`/api/health` returns `live_trading: false`. No private Bitunix order endpoints are implemented.
