#!/usr/bin/env bash
# PR #19: run the complete backend regression in an isolated offline container.
# Never builds, starts, stops, or restarts production services.
set -euo pipefail

S="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test -d "$S/backend/tests"
test -f "$S/docker-compose.yml"
test -d "$S/scripts"
IMAGE="${PR19_TEST_IMAGE:-tradeonbu-tradeonbu:latest}"
docker image inspect "$IMAGE" >/dev/null

T="$(mktemp -d /tmp/tradeonbu-pr19-regression.XXXXXXXX)"
echo "=== PR19 OFFLINE REGRESSION ==="
echo "STAGING_HEAD=$(git -C "$S" rev-parse --short HEAD)"
echo "LOG_FILE=$T/test.log"

set +e
docker run --rm \
  --cpus=1 \
  --memory=1536m \
  --pids-limit=256 \
  --network none \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,size=256m \
  -e DISABLE_APP_BACKGROUND=1 \
  -e SKIP_CONFIG_GUARD=1 \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e MPLCONFIGDIR=/tmp/mpl \
  -e PYTHONPATH=/app:/app/backend \
  -e DATABASE_URL=sqlite+aiosqlite:////app/backend/data/test_tradeonbu.db \
  -v "$S/backend:/app/backend:ro" \
  -v "$S/scripts:/app/scripts:ro" \
  -v "$S/docker-compose.yml:/app/docker-compose.yml:ro" \
  -v "$T:/app/backend/data:rw" \
  -w /app/backend \
  "$IMAGE" \
  python -m pytest tests/ \
  -q --tb=short --disable-warnings -p no:cacheprovider \
  > "$T/test.log" 2>&1
RC=$?
set -e

echo "=== REGRESSION RESULT ==="
tail -n 75 "$T/test.log"
echo "EXIT_CODE=$RC"
echo "LOG_FILE=$T/test.log"
exit "$RC"
