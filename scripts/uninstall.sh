#!/usr/bin/env bash
set -euo pipefail
INSTALL_DIR="${INSTALL_DIR:-/opt/tradeonbu}"
read -rp "Stop containers? [Y/n] " a1
read -rp "DELETE data directory ${INSTALL_DIR}/data ? [yes/N] " a2
cd "$INSTALL_DIR" 2>/dev/null && docker compose down || true
[[ "${a1:-Y}" != "n" && "${a1:-Y}" != "N" ]] || exit 0
if [[ "$a2" == "yes" ]]; then
  rm -rf "${INSTALL_DIR}/data"
  echo "Data removed."
else
  echo "Data preserved at ${INSTALL_DIR}/data"
fi
