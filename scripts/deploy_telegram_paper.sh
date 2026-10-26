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
# Reader starts with export off, allowing a new baseline while the app is built.
(cd "$DEST" && PAPER_EXPORT=0 docker compose up -d --build)
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
# Persistent reader toggle; no credentials are stored in this env file.
python3 - "$DEST/.env" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); lines=p.read_text().splitlines() if p.exists() else []
lines=[x for x in lines if not x.startswith('PAPER_EXPORT=')]
p.write_text('\n'.join(lines+['PAPER_EXPORT=1'])+'\n')
PY
(cd "$DEST" && docker compose up -d)
printf '\nBackup: %s\n' "$BACKUP"
echo 'Deployment complete. Reader re-establishes its baseline; check BehDark status in the Positions page.'
