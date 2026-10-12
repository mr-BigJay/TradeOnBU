#!/usr/bin/env bash
# TradeOnBU interactive production installer — idempotent, no volume destruction.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/tradeonbu}"
REPO="${TRADEONBU_REPO:-https://github.com/mr-BigJay/TradeOnBU.git}"
BRANCH="${TRADEONBU_BRANCH:-cursor/ict-lifecycle-system-2ec7}"
STATE_FILE="${INSTALL_DIR}/.install_state"
LOG_FILE="${TRADEONBU_LOG_FILE:-/var/log/tradeonbu/install.log}"
TTY="${TRADEONBU_TTY:-/dev/tty}"

log() { echo "[install] $*" | tee -a "${LOG_FILE}" 2>/dev/null || echo "[install] $*"; }
die() { log "ERROR: $*"; exit 1; }

load_helpers() {
  if [[ -n "${TRADEONBU_INSTALL_LIB:-}" && -f "$TRADEONBU_INSTALL_LIB" ]]; then
    # shellcheck source=/dev/null
    source "$TRADEONBU_INSTALL_LIB"
  elif [[ -f "$(dirname "$0")/install_lib.sh" ]]; then
    # shellcheck source=scripts/install_lib.sh
    source "$(dirname "$0")/install_lib.sh"
  else
    die "Missing install_lib.sh. Use: curl -fsSL .../scripts/bootstrap-install.sh | sudo bash"
  fi
  declare -F validate_domain >/dev/null 2>&1 || die "install_lib.sh is incomplete (validate_domain missing)"
  declare -F check_os >/dev/null 2>&1 || die "install_lib.sh is incomplete (check_os missing)"
}

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    die "Run as root (e.g. curl -fsSL .../bootstrap-install.sh | sudo bash)"
  fi
}

require_tty() {
  if [[ ! -r "$TTY" ]]; then
    die "Interactive terminal required (/dev/tty). Run: curl -fsSL .../bootstrap-install.sh -o /tmp/tradeonbu-install.sh && sudo bash /tmp/tradeonbu-install.sh"
  fi
}

prompt_inputs() {
  if [[ -f "$STATE_FILE" ]]; then
    # shellcheck source=/dev/null
    source "$STATE_FILE"
    log "Resuming previous install (domain=${DOMAIN:-unset})"
    ADMIN_USER="${ADMIN_USER:-admin}"
    return
  fi
  require_tty
  read -rp "Domain name (e.g. trade.example.com): " DOMAIN <"$TTY"
  validate_domain "$DOMAIN" || die "Invalid domain format"
  read -rp "Administrator username: " ADMIN_USER <"$TTY"
  read -srp "Administrator password: " ADMIN_PASS <"$TTY"
  echo <"$TTY"
  read -srp "Confirm password: " ADMIN_PASS2 <"$TTY"
  echo <"$TTY"
  [[ "$ADMIN_PASS" == "$ADMIN_PASS2" ]] || die "Passwords do not match"
  [[ ${#ADMIN_PASS} -ge 10 ]] || die "Password too short (min 10)"
  unset ADMIN_PASS2
}

check_dns() {
  log "Checking DNS for ${DOMAIN}..."
  local resolved
  resolved=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1 {print $1}')
  if [[ -z "$resolved" ]]; then
    resolved=$(getent ahostsv6 "$DOMAIN" 2>/dev/null | awk 'NR==1 {print $1}')
  fi
  [[ -n "$resolved" ]] || die "DNS lookup failed for ${DOMAIN}"
  local pub
  pub=$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || curl -fsS --max-time 8 ifconfig.me 2>/dev/null || true)
  if [[ -n "$pub" && "$resolved" != "$pub" ]]; then
    log "WARNING: ${DOMAIN} -> ${resolved} but server public IP appears ${pub}"
    log "Fix DNS A/AAAA before expecting a valid certificate."
  else
    log "DNS resolves to ${resolved}"
  fi
}

check_ports() {
  if command -v ss >/dev/null; then
    if ss -tln | grep -q ':80 '; then
      log "Port 80 listener detected (expected for Caddy)"
    else
      log "Port 80 is free (OK before first Caddy start)"
    fi
    if ss -tln | grep -q ':443 '; then
      log "Port 443 listener detected (expected for Caddy)"
    else
      log "Port 443 is free (OK before first Caddy start)"
    fi
  fi
}

install_system_packages() {
  log "Ensuring system packages (git, curl, openssl, python3)..."
  apt-get update -qq
  apt-get install -y git curl openssl ca-certificates python3 dnsutils iproute2 rsync
}

install_docker() {
  if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
    log "Docker already installed"
    return
  fi
  log "Installing Docker Engine..."
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  if [[ -f /etc/os-release ]]; then
    # shellcheck source=/dev/null
    . /etc/os-release
  fi
  if [[ "${ID:-ubuntu}" == "debian" ]]; then
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  else
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  fi
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${ID:-ubuntu} ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  systemctl enable --now docker
}

clone_repo() {
  if [[ -d "${INSTALL_DIR}/.git" ]]; then
    log "Updating existing repository..."
    git -C "$INSTALL_DIR" fetch origin "$BRANCH"
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH" || log "Pull skipped — using current checkout"
    return
  fi
  if [[ -d "$INSTALL_DIR" ]] && [[ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
    log "Non-empty ${INSTALL_DIR} without .git — cloning to temp checkout"
    local tmp
    tmp="$(mktemp -d /tmp/tradeonbu-clone.XXXXXX)"
    git clone --branch "$BRANCH" --depth 1 "$REPO" "$tmp"
    mkdir -p "$INSTALL_DIR"
    rsync -a --exclude data --exclude .env --exclude backups "$tmp/" "$INSTALL_DIR/"
    rm -rf "$tmp"
    return
  fi
  mkdir -p "$INSTALL_DIR"
  log "Cloning repository..."
  git clone --branch "$BRANCH" --depth 1 "$REPO" "$INSTALL_DIR"
}

write_env() {
  local env_file="${INSTALL_DIR}/.env"
  if [[ -f "$env_file" && -f "$STATE_FILE" ]]; then
    log "Preserving existing .env"
    grep -q '^DOMAIN=' "$env_file" || echo "DOMAIN=${DOMAIN}" >> "$env_file"
    sed -i '/^SKIP_CONFIG_GUARD=/d' "$env_file" 2>/dev/null || true
    return
  fi
  local secret_key
  secret_key=$(openssl rand -hex 32)
  cat >"$env_file" <<EOF
DOMAIN=${DOMAIN}
ACME_EMAIL=admin@${DOMAIN}
DATABASE_URL=sqlite+aiosqlite:////app/data/tradeonbu.db
DATA_DIR=/app/data
SESSION_AUTH_ENABLED=1
SESSION_COOKIE_SECURE=1
INTERNAL_SERVICE_KEY=${secret_key}
DASHBOARD_PUBLIC_URL=https://${DOMAIN}
CORS_ALLOWED_ORIGINS=https://${DOMAIN}
EOF
  chmod 600 "$env_file"
  log "Generated restricted .env (Telegram/trading configured in dashboard only)"
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
  if [[ -z "${ADMIN_PASS:-}" ]]; then
    log "Administrator already exists or resume mode — skipping create-admin"
    return
  fi
  printf '%s' "$ADMIN_PASS" | docker compose exec -T tradeonbu python3 -m app.cli create-admin \
    --username "$ADMIN_USER" \
    --domain "$DOMAIN" \
    --password-stdin
  unset ADMIN_PASS
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
  load_helpers
  require_root
  check_os || die "Unsupported OS — Ubuntu/Debian required"
  mkdir -p "$(dirname "$LOG_FILE")"
  mkdir -p "$INSTALL_DIR/data" 2>/dev/null || true
  prompt_inputs
  check_dns
  check_ports
  install_system_packages
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
