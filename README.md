# TradeOnBU — ICT Trend & Signal Lifecycle (Paper)

Rule-based ICT analysis, Telegram inline approval, real Bitunix candlestick charts, and full paper-trading lifecycle. **No live Bitunix orders.**

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
