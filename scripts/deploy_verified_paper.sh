#!/usr/bin/env bash
# Usage: bash scripts/deploy_verified_paper.sh <reviewed commit SHA>
# Run on the VPS checkout. Paper only; no reset/clean, no credential changes.
set -Eeuo pipefail
cd /opt/tradeonbu
target=${1:?Pass the exact reviewed commit SHA}
[[ "$target" =~ ^[0-9a-f]{40}$ ]] || { echo 'Invalid commit SHA'; exit 1; }
test "$(git branch --show-current)" = main
git diff --quiet
git diff --cached --quiet
git cat-file -e "$target^{commit}"
git merge-base --is-ancestor HEAD "$target"

docker compose exec -T tradeonbu python - <<'PY'
import sqlite3
from datetime import datetime,timezone
from pathlib import Path
folder=Path('/app/data/backups');folder.mkdir(exist_ok=True)
path=folder/('before-verified-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'.db')
with sqlite3.connect('file:/app/data/tradeonbu.db?mode=ro',uri=True) as src:
    with sqlite3.connect(path) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Backup failed validation')
print('BACKUP_OK',path)
PY
old_image=$(docker inspect -f '{{.Image}}' "$(docker compose ps -q tradeonbu)")
rollback="tradeonbu-rollback:verified-$(date -u +%Y%m%d-%H%M%S)"
docker image tag "$old_image" "$rollback"
printf 'ROLLBACK_IMAGE=%s\n' "$rollback"
git merge --ff-only "$target"
test "$(git rev-parse HEAD)" = "$target"
docker compose build tradeonbu
docker compose run --rm --no-deps -e DISABLE_APP_BACKGROUND=1 tradeonbu python -c 'from app.main import app; from app.services.verified_trade_plan import enabled; assert enabled(); print("IMPORT_OK")'
docker compose up -d --no-deps --wait --wait-timeout 240 tradeonbu
docker compose exec -T tradeonbu curl -fsS http://127.0.0.1:8000/api/health
printf '\nDEPLOYED %s\n' "$target"
docker compose exec -T tradeonbu python -m app.verified_probe
