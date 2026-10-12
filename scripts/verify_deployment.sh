#!/usr/bin/env bash
# TradeOnBU deployment verification (Ubuntu). Exits non-zero on hard failures.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PASS=0
FAIL=0
SKIP=0

log() { echo "[verify] $*"; }
pass() { log "PASS: $1"; PASS=$((PASS + 1)); }
fail() { log "FAIL: $1"; FAIL=$((FAIL + 1)); }
skip() { log "SKIP: $1"; SKIP=$((SKIP + 1)); }

log "TradeOnBU Phase 2.2 deployment verification"
log "Defaults: BTCUSDT, margin 50 USDT, leverage 10x, min net R:R 2, scan 5m, pending 90m, missed 0.30%, hold 6h"

# --- Docker Compose ---
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  if docker compose up -d --build 2>&1 | tee /tmp/tradeonbu-compose.log; then
    sleep 8
    if docker compose ps 2>/dev/null | grep -qE 'Up|running'; then
      pass "Docker Compose services running"
    else
      fail "Docker Compose did not report running services"
    fi
  else
    fail "Docker Compose up failed (see /tmp/tradeonbu-compose.log)"
  fi
else
  skip "Docker not available — compose startup not tested"
fi

# --- Configuration security ---
if [[ -f .env ]]; then
  if grep -qE '^(DASHBOARD_API_KEY|ADMIN_API_KEY)=' .env; then
    if grep -qE '^(DASHBOARD_API_KEY|ADMIN_API_KEY)=(changeme|test|$)' .env; then
      fail "Weak or placeholder API keys in .env"
    else
      pass "API keys present in .env"
    fi
  else
    fail "Missing DASHBOARD_API_KEY or ADMIN_API_KEY in .env"
  fi
else
  skip ".env not found (use .env.example for production)"
fi

if grep -rq 'live_trading.*true' backend/app 2>/dev/null; then
  fail "live_trading true found in backend"
else
  pass "No live_trading=true in backend sources"
fi

BASE_URL="${VERIFY_BASE_URL:-http://127.0.0.1:8000}"
API_KEY="${DASHBOARD_API_KEY:-}"

health() {
  curl -sf "${BASE_URL}/api/health" 2>/dev/null || return 1
}

if health >/tmp/tradeonbu-health.json 2>&1; then
  if grep -q '"live_trading": false' /tmp/tradeonbu-health.json && grep -q '"paper_only": true' /tmp/tradeonbu-health.json; then
    pass "Health endpoint: paper_only, live_trading disabled"
  else
    fail "Health endpoint missing expected paper-only flags"
  fi
else
  skip "API not reachable at ${BASE_URL} (start stack or set VERIFY_BASE_URL)"
fi

# --- Bitunix public data ---
if curl -sf "https://fapi.bitunix.com/api/v1/futures/market/tickers?symbols=BTCUSDT" >/tmp/tradeonbu-ticker.json 2>&1; then
  pass "Bitunix public ticker reachable"
else
  fail "Bitunix public ticker unreachable"
fi

if [[ -n "$API_KEY" ]] && health >/dev/null 2>&1; then
  if curl -sf -H "X-API-Key: $API_KEY" "${BASE_URL}/api/market/chart?symbol=BTCUSDT&interval=5m" >/tmp/tradeonbu-chart.json; then
    if grep -q 'candles' /tmp/tradeonbu-chart.json; then
      pass "Live chart API returns candles"
    else
      fail "Chart API response missing candles"
    fi
  else
    fail "Chart API request failed"
  fi
else
  skip "Chart API test (set DASHBOARD_API_KEY and running API)"
fi

# --- Telegram (optional) ---
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
  if curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" >/tmp/tradeonbu-tg-me.json; then
    pass "Telegram bot token valid (getMe)"
  else
    fail "Telegram getMe failed"
  fi
  skip "Telegram approval/rejection requires manual inline-button test in chat"
else
  skip "Telegram credentials not set — notification/approval not tested"
fi

# --- Deterministic paper trade (backend tests) ---
if [[ -d backend ]]; then
  log "Running backend test suite (includes paper lifecycle)..."
  (
    cd backend
    export SKIP_CONFIG_GUARD=1
    export DATABASE_URL="${DATABASE_URL:-sqlite+aiosqlite:///./data/verify_tradeonbu.db}"
    export DASHBOARD_API_KEY="${DASHBOARD_API_KEY:-test-dashboard-key-0123456789}"
    export ADMIN_API_KEY="${ADMIN_API_KEY:-test-admin-key-0123456789ab}"
    export TELEGRAM_ALLOWED_USER_IDS="${TELEGRAM_ALLOWED_USER_IDS:-111,222}"
    export DISABLE_APP_BACKGROUND=1
    PYTHONPATH=. pytest tests -q --tb=no 2>&1 | tee /tmp/tradeonbu-pytest.log
  ) && pass "pytest suite green (paper + reliability)" || fail "pytest suite failed (see /tmp/tradeonbu-pytest.log)"
fi

# --- Restart recovery ---
if command -v docker >/dev/null 2>&1 && docker compose ps 2>/dev/null | grep -q Up; then
  docker compose restart 2>&1 | tee /tmp/tradeonbu-restart.log || true
  sleep 6
  if health >/dev/null 2>&1; then
    pass "API healthy after compose restart"
  else
    fail "API not healthy after restart"
  fi
else
  skip "Restart recovery (Docker not running)"
fi

# --- No order execution endpoints ---
if [[ -f backend/app/main.py ]]; then
  if grep -qE '/order|place_order|private' backend/app/main.py; then
    fail "Suspicious order/private routes in main.py"
  else
    pass "No Bitunix order routes in FastAPI main"
  fi
fi

log "Summary: PASS=$PASS FAIL=$FAIL SKIP=$SKIP"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
