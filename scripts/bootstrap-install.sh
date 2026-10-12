#!/usr/bin/env bash
# Entry point for one-line install: downloads installer + helpers to a temp dir, then runs install.sh.
set -euo pipefail

RAW_BASE="${TRADEONBU_RAW_BASE:-https://raw.githubusercontent.com/mr-BigJay/TradeOnBU/cursor/ict-lifecycle-system-2ec7}"
TMP="${TRADEONBU_INSTALL_TMP:-$(mktemp -d /tmp/tradeonbu-install.XXXXXX)}"
export TRADEONBU_INSTALL_TMP="$TMP"

cleanup() {
  if [[ "${TRADEONBU_KEEP_INSTALL_TMP:-0}" != "1" ]]; then
    rm -rf "$TMP"
  fi
}
trap cleanup EXIT

fetch() {
  local rel="$1"
  local dest="$2"
  curl -fsSL "${RAW_BASE}/${rel}" -o "$dest" || {
    echo "[bootstrap] Failed to download ${RAW_BASE}/${rel}" >&2
    exit 1
  }
}

fetch "scripts/install_lib.sh" "$TMP/install_lib.sh"
fetch "scripts/install.sh" "$TMP/install.sh"
chmod +x "$TMP/install.sh"

export TRADEONBU_INSTALL_LIB="$TMP/install_lib.sh"
exec bash "$TMP/install.sh" "$@"
