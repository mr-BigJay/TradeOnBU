# TradeOnBU — ICT Trend & Signal Lifecycle (Paper)

Rule-based ICT analysis and full signal lifecycle management for Bitunix **market data only**. No live orders are placed.

## Features

- **Trend engine**: 4H / 15M / 5M structure (swing, BOS, MSS, liquidity) → `BULLISH | BEARISH | SIDEWAYS | UNCERTAIN`
- **Smart entry scan** every 5 minutes (configurable): FVG + structure, net R:R ≥ 2 after fees/slippage/spread
- **State machine**: `CREATED → WAITING_FOR_ENTRY → ACTIVE → TP_HIT | SL_HIT | TIME_EXIT` plus `MISSED_ENTRY`, `EXPIRED`, `INVALIDATED`, `REJECTED`, `DATA_ERROR`
- **Pending rules**: 90m default expiry, 0.30% missed-entry move, 6h active time limit (editable in Trade Monitor)
- **Telegram** alerts (optional) with dashboard approval before monitoring starts
- **ChatGPT manual import** form (no OpenAI API on server)
- **Trade Monitor** dashboard with chart lines for Entry / SL / TP and full event history

## Quick start (Ubuntu)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cd frontend && npm install && npm run build && cd ..
mkdir -p data
cd backend && uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` for Trade Monitor. API docs: `/docs`.

### Docker (24/7)

```bash
cp .env.example .env
docker compose up -d --build
```

## Configuration

| Setting | Default | Dashboard |
|--------|---------|-----------|
| Max pending time | 90 min | Yes |
| Missed entry distance | 0.30% | Yes |
| Holding time limit | 6 h | Yes |
| Min net R:R | 2.0 | Yes |

Paper assumptions: **50 USDT margin**, **10x** leverage (shown in Telegram messages).

## Architecture (future live trading)

`PaperExecutionEngine` and `MarketDataService` are isolated from order placement. A future `BitunixExecutionAdapter` can hook into the same lifecycle transitions after manual user confirmation.

## Tests

```bash
cd backend && PYTHONPATH=. pytest tests -q
```
