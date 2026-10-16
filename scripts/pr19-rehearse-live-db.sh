#!/usr/bin/env bash
# Run ONLY on the host holding TradeOnBU. Backup live SQLite via SQLite's
# online backup API, then migrate and validate a disposable copy in offline Docker.
# NEVER mounts, modifies or restarts the production application/database.
set -euo pipefail
umask 077

S="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROD_DB="${TRADEONBU_PROD_DB:-/opt/tradeonbu/data/tradeonbu.db}"
IMAGE="${PR19_TEST_IMAGE:-tradeonbu-tradeonbu:latest}"

if [ ! -d "$S/backend" ] || [ ! -f "$S/scripts/pr19-check-sqlite-upgrade.py" ]; then
  echo "STOP: PR19 staging checkout is incomplete"
  exit 1
fi
if [ ! -f "$PROD_DB" ] || [ -L "$PROD_DB" ]; then
  echo "STOP: Live database is absent or a symlink: $PROD_DB"
  exit 1
fi
REAL_DB="$(readlink -f "$PROD_DB")"
if [ "$REAL_DB" != "/opt/tradeonbu/data/tradeonbu.db" ]; then
  echo "STOP: Refusing an unexpected production DB path: $REAL_DB"
  exit 1
fi
docker image inspect "$IMAGE" >/dev/null

T="$(mktemp -d /tmp/tradeonbu-pr19-db-check.XXXXXXXX)"
chmod 700 "$T"

echo "=== SQLITE LIVE SNAPSHOT (READ ONLY) ==="
echo "SOURCE=$REAL_DB"
echo "TEST_DIR=$T"
echo "STAGING_HEAD=$(git -C "$S" rev-parse --short HEAD)"

python3 - "$REAL_DB" "$T/live-backup.db" <<'PY'
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

source_path, backup_path = map(Path, sys.argv[1:])
with source_path.open("rb") as f:
    if f.read(16) != b"SQLite format 3\0":
        raise SystemExit("STOP: Source file is not a valid SQLite database")
source_uri = "file:" + quote(str(source_path), safe="/") + "?mode=ro"
with sqlite3.connect(source_uri, uri=True, timeout=30) as live:
    with sqlite3.connect(str(backup_path), timeout=30) as snap:
        live.backup(snap, pages=500, sleep=0.1)
        assert snap.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
backup_path.chmod(0o600)
print("SQLite backup completed; live database was opened read-only.")
PY

cp "$T/live-backup.db" "$T/test_tradeonbu.db"
chmod 600 "$T/test_tradeonbu.db"

echo "=== OFFLINE MIGRATION DRY RUN ==="
set +e
docker run --rm \
  --cpus=1 --memory=1536m --pids-limit=256 \
  --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,size=128m \
  -e DISABLE_APP_BACKGROUND=1 \
  -e SKIP_CONFIG_GUARD=1 \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e DATABASE_URL=sqlite+aiosqlite:////app/data/test_tradeonbu.db \
  -v "$S/backend:/app/backend:ro" \
  -v "$S/scripts:/app/scripts:ro" \
  -v "$T:/app/data:rw" \
  -w /app/backend \
  "$IMAGE" python /app/scripts/pr19-check-sqlite-upgrade.py \
  > "$T/result.log" 2>&1
RC=$?
set -e

cat "$T/result.log"
echo "EXIT_CODE=$RC"
echo "LOCAL_BACKUP=$T/live-backup.db"
echo "DETAILS=$T/result.log"
exit "$RC"
