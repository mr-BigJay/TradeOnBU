#!/usr/bin/env bash
# Run as root with an explicitly reviewed commit SHA already fetched in /opt/tradeonbu.
set -Eeuo pipefail
umask 077
TARGET="${1:?Pass the release commit SHA}"
[[ "$TARGET" =~ ^[0-9a-f]{40}$ ]]
cd /opt/tradeonbu
test "$(git branch --show-current)" = main
git diff --quiet
git diff --cached --quiet
git cat-file -e "$TARGET^{commit}"
git merge-base --is-ancestor HEAD "$TARGET"
OVERRIDE=docker-compose.override.yml
if [ -f "$OVERRIDE" ] && ! grep -q '^# TradeOnBU Telegram paper integration$' "$OVERRIDE"; then
    echo 'STOP: existing compose override must be reviewed before installation.'
    exit 1
fi
BACKUP="/root/tradeonbu-telegram-paper-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
cp -p .env "$BACKUP/.env"
[ ! -f /opt/tradeonbu-telegram-reader/.env ] || cp -p /opt/tradeonbu-telegram-reader/.env "$BACKUP/reader.env"
[ ! -f "$OVERRIDE" ] || cp -p "$OVERRIDE" "$BACKUP/$OVERRIDE"
git rev-parse HEAD > "$BACKUP/previous-commit"
python3 - "$BACKUP" <<'PY'
import sqlite3,sys
from pathlib import Path
src=sqlite3.connect('file:data/tradeonbu.db?mode=ro',uri=True)
dst=sqlite3.connect(str(Path(sys.argv[1])/'tradeonbu.db'))
with dst: src.backup(dst)
dst.close(); src.close()
PY
git merge --ff-only "$TARGET"
DEST=/opt/tradeonbu-telegram-reader
mkdir -p "$DEST"
git archive "$TARGET" integrations/telegram-web-reader | tar -x -C "$DEST" --strip-components=2
# Persist the resource budget verified on this server, including after recreation.
set_reader_env() {
python3 - "$DEST/.env" "$1" <<'PYENV'
from pathlib import Path
import sys
p=Path(sys.argv[1]); lines=p.read_text().splitlines() if p.exists() else []
values={'READER_HEADLESS':'1','PAPER_EXPORT':sys.argv[2],
        'READER_CPUS':'0.75','READER_MEMORY_LIMIT':'1g'}
lines=[x for x in lines if x.split('=',1)[0].strip() not in values]
p.write_text('\n'.join(lines+[f'{k}={v}' for k,v in values.items()])+'\n')
PYENV
}
SUCCESS=0
cleanup() {
    if [ "$SUCCESS" != 1 ]; then
        set_reader_env 0 || true
        (cd "$DEST" && docker compose stop reader) || true
        echo "STOP: reader stopped; export disabled. Backup: $BACKUP"
    fi
}
trap cleanup EXIT
restart_reader() {
    (cd "$DEST" && docker compose stop reader)
    if [ -n "$(docker ps -q --filter volume=tradeonbu-telegram-reader_reader_state)" ]; then
        echo 'STOP: another container is using the browser profile.'
        return 1
    fi
    (cd "$DEST" && docker compose run --rm --no-deps -T --entrypoint python reader - <<'PYLOCK'
from pathlib import Path
for name in ('SingletonLock','SingletonSocket','SingletonCookie'):
    p=Path('/state/profile')/name
    if p.is_symlink(): p.unlink()
    elif p.exists(): raise SystemExit('STOP: unexpected non-symlink lock')
Path('/state/status.json').unlink(missing_ok=True)
PYLOCK
    )
    (cd "$DEST" && docker compose up -d reader)
}
wait_for_reader() {
    local good=0 previous='' current=''
    for attempt in $(seq 1 40); do
        current="$(cd "$DEST" && timeout 15 docker compose exec -T reader python -c '
import json,time
from pathlib import Path
s=json.loads(Path("/state/status.json").read_text())
assert s.get("state")=="observing" and s.get("headless") is True
assert 0 <= time.time()-s["heartbeat"] < 60
print(s["heartbeat"])
' 2>/dev/null)" || current=''
        if [ -n "$current" ] && [ "$current" != "$previous" ]; then
            good=$((good+1)); previous="$current"
            if [ "$good" -ge 2 ]; then return 0; fi
        elif [ -z "$current" ]; then
            good=0
        fi
        sleep 10
    done
    echo 'STOP: reader did not establish fresh channel scans.'
    return 1
}
set_reader_env 0
(cd "$DEST" && docker compose build reader)
restart_reader
wait_for_reader
{
  echo '# TradeOnBU Telegram paper integration'
  cat deploy/telegram-paper.compose.yml
} > "$OVERRIDE"
python3 - <<'PY'
from pathlib import Path
p=Path('.env'); lines=p.read_text().splitlines()
lines=[x for x in lines if not x.startswith('TELEGRAM_PAPER_ENABLED=')]
p.write_text('\n'.join(lines+['TELEGRAM_PAPER_ENABLED=1'])+'\n')
PY
docker compose build tradeonbu
docker compose up -d --no-deps --wait --wait-timeout 180 tradeonbu
docker compose exec -T tradeonbu curl -fsS --max-time 10 http://127.0.0.1:8000/api/health
# Re-baseline with export enabled only after the application is healthy.
set_reader_env 1
restart_reader
wait_for_reader
SUCCESS=1
printf '\nBackup: %s\n' "$BACKUP"
echo 'Deployment complete. Check BehDark status in the Positions page.'
(cd "$DEST" && docker compose exec -T reader python /reader/reader.py status)
docker stats --no-stream
