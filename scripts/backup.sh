#!/usr/bin/env bash
set -euo pipefail
INSTALL_DIR="${INSTALL_DIR:-/opt/tradeonbu}"
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="${INSTALL_DIR}/backups/tradeonbu-${STAMP}.tar.gz"
mkdir -p "${INSTALL_DIR}/backups"
tar -czf "$OUT" -C "$INSTALL_DIR" data .env deploy/Caddyfile 2>/dev/null || tar -czf "$OUT" -C "$INSTALL_DIR" data .env
chmod 600 "$OUT"
echo "Backup written: $OUT"
