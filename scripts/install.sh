#!/usr/bin/env bash
# TradeOnBU interactive production installer — idempotent, no volume destruction.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/tradeonbu}"
REPO="${TRADEONBU_REPO:-https://github.com/mr-BigJay/TradeOnBU.git}"
BRANCH="${TRADEONBU_BRANCH:-cursor/ict-lifecycle-system-2ec7}"
STATE_FILE="${INSTALL_DIR}/.install_state"
LOG_FILE="${INSTALL_DIR}/install.log"

log() { echo "[install] $*" | tee -a "${LOG_FILE}" 2>/dev/null || echo "[install] $*"; }
die() { log "ERROR: $*"; exit 1; }

# shellcheck source=scripts/install_lib.sh
source "$(dirname "$0")/install_lib.sh" 2>/dev/null || true

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    die "Run with sudo: curl -fsSL ... | sudo bash"
  fi
}

check_os || die "Unsupported OS — Ubuntu/Debian required"

prompt_inputs() {
  if [[ -f "$STATE_FILE" ]]; then
    # shellcheck source=/dev/null
    source "$STATE_FILE"
    log "Resuming previous install (domain=${DOMAIN:-unset})"
    return
  fi
  read -rp "Domain name (e.g. trade.example.com): " DOMAIN
  validate_domain "$DOMAIN" || die "Invalid domain format"
  read -rp "Administrator username: " ADMIN_USER
  read -srp "Administrator password: " ADMIN_PASS; echo
  read -srp "Confirm password: " ADMIN_PASS2; echo
  [[ "$ADMIN_PASS" == "$ADMIN_PASS2" ]] || die "Passwords do not match"
  [[ ${#ADMIN_PASS} -ge 10 ]] || die "Password too short (min 10)"
}

check_dns() {
  log "Checking DNS for ${DOMAIN}..."
  local resolved
  resolved=$(getent ahosts "$DOMAIN" | awk '/STREAM/ {print $1; exit}')
  [[ -n "$resolved" ]] || die "DNS lookup failed for ${DOMAIN}"
  local pub
  pub=$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || curl -fsS --max-time 5 ifconfig.me 2>/dev/null || true)
  if [[ -n "$pub" && "$resolved" != "$pub" ]]; then
    log "WARNING: ${DOMAIN} resolves to ${resolved} but this server public IP appears ${pub}"
    log "Fix DNS A/AAAA before expecting a valid certificate."
  else
    log "DNS resolves to ${resolved}"
  fi
}

check_ports() {
  if command -v ss >/dev/null; then
    ss -tln | grep -q ':80 ' && log "Port 80 listener detected (expected for Caddy)"
    ss -tln | grep -q ':443 ' && log "Port 443 listener detected (expected for Caddy)"
  fi
}

install_docker() {
  if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
    log "Docker already installed"
    return
  fi
  log "Installing Docker Engine..."
  apt-get update -qq
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null || \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  # shellcheck source=/dev/null
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  systemctl enable --now docker
}

clone_repo() {
  mkdir -p "$INSTALL_DIR"
  if [[ -d "${INSTALL_DIR}/.git" ]]; then
    log "Updating existing repository..."
    git -C "$INSTALL_DIR" fetch origin "$BRANCH"
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH" || log "Pull skipped — using current checkout"
  else
    log "Cloning repository..."
    git clone --branch "$BRANCH" --depth 1 "$REPO" "$INSTALL_DIR"
  fi
}

write_env() {
  local env_file="${INSTALL_DIR}/.env"
  if [[ -f "$env_file" && -f "$STATE_FILE" ]]; then
    log "Preserving existing .env"
    grep -q '^DOMAIN=' "$env_file" || echo "DOMAIN=${DOMAIN}" >> "$env_file"
    return
  fi
  local secret_key session_secret
  secret_key=$(openssl rand -hex 32)
  session_secret=$(openssl rand -hex 32)
  cat >"$env_file" <<EOF
DOMAIN=${DOMAIN}
ACME_EMAIL=admin@${DOMAIN}
DATABASE_URL=sqlite+aiosqlite:////app/data/tradeonbu.db
DATA_DIR=/app/data
SESSION_AUTH_ENABLED=1
SESSION_COOKIE_SECURE=1
SKIP_CONFIG_GUARD=1
INTERNAL_SERVICE_KEY=${secret_key}
EOF
  chmod 600 "$env_file"
  log "Generated restricted .env (no Telegram/trading params — configure in dashboard)"
}

start_stack() {
  mkdir -p "${INSTALL_DIR}/data"
  chmod 700 "${INSTALL_DIR}/data"
  cd "$INSTALL_DIR"
  export DOMAIN
  docker compose up -d --build
}

wait_healthy() {
  local i
  for i in $(seq 1 60); do
    if docker compose exec -T tradeonbu curl -sf http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
      log "Application health OK"
      return 0
    fi
    sleep 3
  done
  die "Application failed to become healthy — check: docker compose logs tradeonbu"
}

create_admin() {
  cd "$INSTALL_DIR"
  docker compose exec -T tradeonbu python3 -m app.cli create-admin \
    --username "$ADMIN_USER" \
    --password "$ADMIN_PASS" \
    --domain "$DOMAIN"
}

verify_https() {
  local url="https://${DOMAIN}/api/health"
  log "Waiting for HTTPS (may take 1-2 minutes for first certificate)..."
  local i
  for i in $(seq 1 40); do
    if curl -fsS --max-time 10 "$url" 2>/dev/null | python3 -c "
import sys, json
d=json.load(sys.stdin)
assert d.get('paper_only') is True and d.get('live_trading') is False
" 2>/dev/null; then
      log "HTTPS health check passed"
      return 0
    fi
    sleep 5
  done
  die "HTTPS verification failed — check DNS, ports 80/443, and: docker compose logs caddy"
}

save_state() {
  cat >"$STATE_FILE" <<EOF
DOMAIN=${DOMAIN}
ADMIN_USER=${ADMIN_USER}
INSTALLED=1
EOF
  chmod 600 "$STATE_FILE"
}

print_success() {
  cat <<EOF

-----------------------------------------
TradeOnBU Installed Successfully
-----------------------------------------

Dashboard:
https://${DOMAIN}

Username:
${ADMIN_USER}

SSL:
ACTIVE

Trading Mode:
PAPER ONLY

Default Margin:
50 USDT

Default Leverage:
10x

Scanner Interval:
5 minutes

Next Step:
Open the dashboard and configure Telegram under Settings.

-----------------------------------------
EOF
}

main() {
  require_root
  mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
  prompt_inputs
  check_dns
  check_ports
  install_docker
  clone_repo
  write_env
  start_stack
  wait_healthy
  create_admin
  verify_https
  save_state
  print_success
}

main "$@"
