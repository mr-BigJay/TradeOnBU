#!/usr/bin/env bash
set -euo pipefail
INSTALL_DIR="${INSTALL_DIR:-/opt/tradeonbu}"
cd "$INSTALL_DIR"
git fetch origin
git pull --ff-only
docker compose up -d --build
echo "Update complete — data and caddy volumes preserved."
