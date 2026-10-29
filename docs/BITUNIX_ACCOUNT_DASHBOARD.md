# Bitunix live account monitoring (read-only)

The new **حساب Bitunix** navigation item displays a real, administrator-only BTCUSDT USDT-M futures snapshot, independent of simulated positions and BehDark. All requests are signed private **GET** calls; no order, cancel, withdrawal, leverage-change, or TP/SL modification paths exist here. The app remains \`paper_only=true\`, \`live_trading=false\`.

## Safety and credential setup

The existing one-shot probe uses hidden input and does not store secrets. A persistent dashboard needs a server-side credential. Prefer a newly rotated, IP-bound API key and **never** enable Withdrawal permission. Since Bitunix currently offers a Trade-capable key in its UI, its permission may allow real trades outside this dashboard; store and protect it accordingly.

1. SSH to the production server and \`cd /opt/tradeonbu\`. Review the branch and deploy. Do not paste API keys or secrets into GitHub, ChatGPT, a browser form, command arguments, logs or screenshots.
2. Set \`BITUNIX_MONITOR_API_KEY\` and \`BITUNIX_MONITOR_SECRET_KEY\` **only in the server's existing \`.env\`**, restricted to root (\`chmod 600 .env\`). The file must not be committed. Avoid logging/printing the environment or using \`docker compose config\` in shared terminals.
3. Use the interactive script below to safely add/replace both variables without exposing values on-screen or in shell history. Run it only on your trusted SSH terminal.

\`\`\`bash
cd /opt/tradeonbu
umask 077
chmod 600 .env
python3 - <<'PY'
import getpass
import json
import os
from pathlib import Path

p = Path(".env")
assert p.is_file() and not p.is_symlink(), "Missing or unsafe .env"
key = getpass.getpass("Bitunix API Key (hidden): ").strip()
secret = getpass.getpass("Bitunix Secret Key (hidden): ").strip()
assert key and secret, "Both values are required"
lines = p.read_text().splitlines(keepends=True)
names = {"BITUNIX_MONITOR_API_KEY", "BITUNIX_MONITOR_SECRET_KEY"}
lines = [line for line in lines if line.split("=", 1)[0].strip() not in names]
if lines and not lines[-1].endswith("\n"):
    lines[-1] += "\n"
lines += [
    "BITUNIX_MONITOR_API_KEY=" + json.dumps(key) + "\n",
    "BITUNIX_MONITOR_SECRET_KEY=" + json.dumps(secret) + "\n",
]
fd = os.open(str(p), os.O_WRONLY | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.writelines(lines)
os.chmod(p, 0o600)
print("Credentials saved on server; values were not displayed.")
PY
\`\`\`

4. Recreate the main container to read the new environment: \`docker compose up -d --force-recreate --no-deps tradeonbu\`. The new page automatically queries every 45 seconds while visible, with a manual refresh option. Credentials are never sent to the browser and there is no UI field to enter them.
5. Admin session required: middleware also enforces this for \`/api/gui/bitunix\`. Other users receive 403 (session) or 401 (no login). The endpoint sets \`Cache-Control: no-store\`. If the exchange API fails, the page clears the old snapshot rather than showing stale financial balances.

## What the page shows

- USDT account available, frozen, position margin, isolated + cross floating PnL, transferable, bonus, HEDGE/ONE_WAY.
- BTCUSDT **symbol's configured leverage/margin mode**, distinct from each position's and order's actual leverage/margin mode.
- All current BTCUSDT positions: LONG/SHORT, size, entry, position margin, floating and realized PnL, approximate ROI, liquidation price, margin rate, fees and funding.
- Pending regular orders and independent conditional TP/SL orders; BTCUSDT pending TP/SL does **not** imply full stop coverage.
- Up to 20 most recent historical BTCUSDT positions and each realized PnL, fees and funding. RealizedPNL excludes fees and funding according to Bitunix docs; it must **not** be misreported as net PnL.
- Timestamps displayed in Tehran time.

Only BTCUSDT is monitored. This is not a complete multi-asset risk or account history view.

Official reference: https://www.bitunix.com/api-docs/futures/account/get_single_account.html
