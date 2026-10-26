#!/usr/bin/env bash
# Only repairs the separate reader; no app checkout changes or paper activation.
set -Eeuo pipefail
umask 077
TARGET="${1:?Pass release SHA}"
[[ "$TARGET" =~ ^[0-9a-f]{40}$ ]]
REPO=/opt/tradeonbu
DEST=/opt/tradeonbu-telegram-reader
REPORT="/root/tradeonbu-reader-check-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee "$REPORT") 2>&1
cd "$DEST"
# Stop on unexpected failures as well; keep profile/session volume intact.
SUCCESS=0
cleanup() {
  if [ "$SUCCESS" != 1 ]; then
    docker compose stop reader || true
    echo 'RESULT: reader stopped; paper export remains OFF.'
  fi
  echo "REPORT: $REPORT"
}
trap cleanup EXIT

docker compose stop reader
if [ -n "$(docker ps -q --filter volume=tradeonbu-telegram-reader_reader_state)" ]; then
  echo 'STOP: another container uses the profile.'; exit 1
fi
# Preserve installed reader files/env before replacement; account profile is not exported.
BACKUP="/root/tradeonbu-reader-files-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
for file in reader.py start.sh compose.yaml Dockerfile .env; do
  [ ! -f "$file" ] || cp -p "$file" "$BACKUP/"
done
git -C "$REPO" archive "$TARGET" integrations/telegram-web-reader | tar -x -C "$DEST" --strip-components=2
python3 - <<'PY'
from pathlib import Path
p=Path('.env'); lines=p.read_text().splitlines() if p.exists() else []
lines=[s for s in lines if not s.startswith(('READER_HEADLESS=','PAPER_EXPORT='))]
p.write_text('\n'.join(lines+['READER_HEADLESS=1','PAPER_EXPORT=0'])+'\n')
PY
docker compose build reader
docker compose run --rm --no-deps -T --entrypoint python reader - <<'PY'
from pathlib import Path
for name in ('SingletonLock','SingletonSocket','SingletonCookie'):
    p=Path('/state/profile')/name
    if p.is_symlink():
        p.unlink(); print('Removed stale lock:', name)
    elif p.exists():
        raise SystemExit('Unexpected non-symlink lock; stopped')
# Do not interpret a previous process heartbeat as this startup's result.
Path('/state/status.json').unlink(missing_ok=True)
PY
docker compose up -d reader
CID="$(docker compose ps -q reader)"
GOOD=0
HOT=0
# Twenty-four bounded checks including one settings reload, plus command timeouts; no wait for a new post.
for iteration in $(seq 1 24); do
  sleep 20
  STATUS="$(timeout 10 docker compose exec -T reader python /reader/reader.py status 2>/dev/null || true)"
  echo "CHECK $iteration: ${STATUS:-status_unavailable}"
  CPU="$(timeout 10 docker stats --no-stream --format '{{.CPUPerc}}' "$CID" 2>/dev/null || true)"
  echo "CPU: ${CPU:-unavailable}"
  STATE="$(printf '%s' "$STATUS" | python3 -c 'import json,sys,time
try:
 d=json.load(sys.stdin); print(d["state"] if 0<=time.time()-d["heartbeat"]<60 else "stale")
except Exception: print("unavailable")')"
  if [ "$STATE" = power_saving_failed ]; then
    echo 'Power Saving UI could not be verified; no private settings were modified directly.'; break
  fi
  if [ "$STATE" = login_required ]; then
    echo 'Manual Telegram login is required; no credentials were read.'; break
  fi
  if [ "$STATE" = observing ]; then
    if python3 - "$CPU" <<'PY'
import sys
try: value=float(sys.argv[1].rstrip('%'))
except ValueError: sys.exit(1)
sys.exit(0 if 0<=value<20 else 1)
PY
    then GOOD=$((GOOD+1)); HOT=0
    else GOOD=0; HOT=$((HOT+1))
    fi
  else GOOD=0
  fi
  if [ "$GOOD" -ge 3 ]; then
    SUCCESS=1
    echo 'RESULT: observing with CPU below 20% in three consecutive samples; reader left running. Paper export OFF.'
    break
  fi
  if [ "$HOT" -ge 3 ]; then
    echo 'RESULT: observing but CPU remains high or unavailable; reader will be stopped.'; break
  fi
done
docker stats --no-stream || true
# Do not dump browser logs, channel texts, cookies or login screenshots into this report.
