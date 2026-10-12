#!/usr/bin/env bash
set -euo pipefail
ARCHIVE="${1:-}"
[[ -f "$ARCHIVE" ]] || { echo "Usage: $0 /path/to/backup.tar.gz"; exit 1; }
read -rp "Restore will overwrite data/. Continue? [yes/N] " ans
[[ "$ans" == "yes" ]] || exit 1
INSTALL_DIR="${INSTALL_DIR:-/opt/tradeonbu}"
tar -xzf "$ARCHIVE" -C "$INSTALL_DIR"
cd "$INSTALL_DIR"
docker compose restart tradeonbu
echo "Restore applied — verify dashboard and signals."
