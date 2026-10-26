#!/usr/bin/env bash
# Independent BehDark install. Run as root with a fetched, reviewed release SHA.
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
DEST=/opt/tradeonbu-telegram-reader
OVERRIDE=docker-compose.override.yml
if [ -f "$OVERRIDE" ] && ! grep -qx '# TradeOnBU independent BehDark' "$OVERRIDE"; then
    echo 'STOP: existing Compose override needs review; nothing was changed.'
    exit 1
fi
# Never enable the superseded shared-wallet integration alongside this service.
if grep -Eq '^TELEGRAM_PAPER_ENABLED=(1|true)$' .env; then
    echo 'STOP: legacy shared-wallet integration is enabled; review migration first.'
    exit 1
fi
BACKUP="/root/tradeonbu-behdark-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
cp -p .env "$BACKUP/main.env"
[ ! -f "$OVERRIDE" ] || cp -p "$OVERRIDE" "$BACKUP/$OVERRIDE"
[ ! -f "$DEST/.env" ] || cp -p "$DEST/.env" "$BACKUP/reader.env"
git rev-parse HEAD > "$BACKUP/previous-commit"
python3 - "$BACKUP" <<'PY'
import sqlite3,sys
from pathlib import Path
src=sqlite3.connect('file:data/tradeonbu.db?mode=ro',uri=True)
dst=sqlite3.connect(str(Path(sys.argv[1])/'tradeonbu.db'))
with dst: src.backup(dst)
dst.close();src.close()
PY
# If already installed, snapshot its independent ledger before changing images.
if docker volume inspect tradeonbu_behdark_data >/dev/null 2>&1; then
    if [ -z "$(docker compose ps -q behdark 2>/dev/null)" ]; then
        echo 'STOP: existing BehDark volume with stopped service; back it up before upgrade.'
        exit 1
    fi
    docker compose exec -T behdark python - <<'PY'
import sqlite3
src=sqlite3.connect('/behdark-data/behdark.db')
dst=sqlite3.connect('/behdark-data/pre-upgrade.db')
with dst: src.backup(dst)
dst.close();src.close()
PY
    docker compose cp behdark:/behdark-data/pre-upgrade.db "$BACKUP/behdark.db"
fi
git merge --ff-only "$TARGET"
mkdir -p "$DEST"
git archive "$TARGET" integrations/telegram-web-reader | tar -x -C "$DEST" --strip-components=2
python3 - "$DEST/.env" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]);lines=p.read_text().splitlines() if p.exists() else []
values={'READER_HEADLESS':'1','PAPER_EXPORT':'1','READER_CPUS':'0.75','READER_MEMORY_LIMIT':'1g'}
lines=[x for x in lines if x.split('=',1)[0].strip() not in values]
p.write_text('\n'.join(lines+[f'{k}={v}' for k,v in values.items()])+'\n')
PY
{
    echo '# TradeOnBU independent BehDark'
    cat deploy/behdark.compose.yml
} > "$OVERRIDE"
# Build before interrupting the existing browser or dashboard.
(cd "$DEST" && docker compose build reader)
docker compose build behdark tradeonbu
(cd "$DEST" && docker compose stop reader)
if [ -n "$(docker ps -q --filter volume=tradeonbu-telegram-reader_reader_state)" ]; then
    echo 'STOP: browser profile is in use by another container.'
    exit 1
fi
(cd "$DEST" && docker compose run --rm --no-deps -T --entrypoint python reader - <<'PY'
from pathlib import Path
for name in ('SingletonLock','SingletonSocket','SingletonCookie'):
    p=Path('/state/profile')/name
    if p.is_symlink(): p.unlink()
    elif p.exists(): raise SystemExit('STOP: unexpected non-symlink lock')
Path('/state/status.json').unlink(missing_ok=True)
PY
)
(cd "$DEST" && docker compose up -d reader)
# No wallet balance and new entries disabled by default on first installation.
docker compose up -d --no-deps --wait --wait-timeout 180 behdark
docker compose up -d --no-deps --wait --wait-timeout 180 tradeonbu
docker compose exec -T tradeonbu curl -fsS --max-time 10 http://127.0.0.1:8000/api/health
printf '\nBackup: %s\n' "$BACKUP"
echo 'Installed. Open BehDark > Wallet, initialize virtual capital, then enable entries.'
(cd "$DEST" && docker compose exec -T reader python /reader/reader.py status)
docker compose ps
