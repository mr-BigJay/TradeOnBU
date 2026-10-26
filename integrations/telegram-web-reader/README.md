# Server-only Telegram Web visibility probe (experimental)

Runs a dedicated Chromium browser on the VPS, with a persistent profile. No Telegram developer API credentials are needed. Sign in manually using the server browser over an SSH tunnel. Once authorized, the browser can remain connected without the phone being online; Telegram can revoke the session and require login again.

**This is a read-only feasibility test, not a production signal feed.** There is no parsing into orders, AI call, exchange access, or TradeOnBU database access. Protected-channel compatibility is unverified. It reads rendered text only and does not forward, download protected media, take screenshots, use OCR, or access Telegram internal stores/APIs. If the normal client cannot expose a message, this probe cannot import it.

## Start

Run in this directory on a Docker Compose VPS:

```sh
docker compose up -d --build
docker compose ps
```

Initial installation downloads Chromium and supporting packages. Allow approximately 1.5 GB available RAM for this container in addition to the trading app. This limit is provisional; measure real usage with `docker stats`.

On your own computer, keep an SSH tunnel open (replace SERVER_IP):

```sh
ssh -N -L 6080:127.0.0.1:6080 root@SERVER_IP
```

Open http://127.0.0.1:6080/vnc.html and press Connect. Sign into Telegram yourself and open **BehDark (VIP)**. Open the most recent messages. The expected Web K address is `https://web.telegram.org/k/#-1878414449`. The reader requires both this numeric channel ID and the exact title; a title change pauses collection. Login credentials are entered in Telegram's browser UI, never in the reader or ChatGPT.

**Do not expose port 6080 publicly.** The viewer has no additional password and is restricted to server loopback by Compose; local server users can access it while enabled. Access externally only through SSH. The container browser profile is an account credential: keep the volume private and never upload it, the SQLite file, or private message bodies to GitHub/support chats. Chromium uses Playwright's default disabled browser sandbox, inside an unprivileged container with all capabilities dropped. Do not browse arbitrary sites in it.

Inspect safe status:

```sh
docker compose exec -T reader python /reader/reader.py status
```

- `observing`: correct channel matched and rendered message elements were found. This does **not** prove complete delivery or signal parsing.
- `waiting_for_channel`: login/opening the exact channel is still needed, or title changed.
- `wrong_channel`, `peer_mismatch`, `selector_mismatch`: collection paused; verify channel/UI, do not weaken ID checks.
- `no_readable_messages`: no recognized message elements yet.
- `offline`, `browser_error`: collection is not confirmed. Heartbeat health only proves the loop is alive, not that Telegram is delivering fresh messages.

`counts` are for the last poll, so zero counts between posts are normal. `stored` is the current deduplicated row count. First observed messages after startup or interruption are a baseline, never a new trading event. Text-free posts are marked unsupported; captions/text are not image/chart interpretation. No historical backfill, deleted-message tracking, complete album handling, or exactly-once delivery guarantee. Web K virtualizes messages and changes its UI; posts during outages may be missed. Do not connect this probe directly to order execution.

After login, disable the interactive viewer (within three seconds) while keeping collection running:

```sh
docker compose exec -T reader touch /state/viewer.disabled
```

For a later manual re-login:

```sh
docker compose exec -T reader rm -f /state/viewer.disabled
docker compose restart reader
```

The persistent named volume survives restarts/rebuilds. It stores the session plus at most 500 latest message records, pruned to seven days on successful scans. These contain private text, but routine logs/status never include it. Stop with `docker compose down`; do not use `down -v` unless intentionally deleting the session and test records. To remove account authorization, revoke this browser session in Telegram Settings > Devices as well.

## Acceptance before any integration

1. Verify `observing` with the exact channel open.
2. Observe a naturally arriving new post and check `stored`/the local database; do not publish private contents in issue logs.
3. Check text and media-only posts, edits, restart, disconnect/reconnect, and the disabled viewer.
4. Measure CPU/RAM and confirm the trading app remains healthy.
5. Only after this passes, design explicit signal parsing, stale-message rejection, persistent event deduplication, cancellation/update semantics, and paper-only execution with risk checks.

No OpenAI API calls are made by this probe. It consumes server CPU/RAM/network. Continuous unattended operation is the goal, not a verified guarantee.

## Validation and references

```sh
python -m unittest discover -s integrations/telegram-web-reader -v
```

Storage unit tests cover baseline, deduplication, edits, unsupported bodies, invalid IDs, retention and bounds. Container launch and the real private channel require the server acceptance test; they were not run in the development environment (Docker unavailable).

Selectors were checked against the official Web K source, not a live private account:
- https://github.com/morethanwords/tweb/blob/master/src/components/chat/bubbles.ts
- https://github.com/morethanwords/tweb/blob/master/src/components/chat/topbar.ts
- https://github.com/morethanwords/tweb/blob/master/src/helpers/peerIdPolyfill.ts
- https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context
- https://core.telegram.org/api/content-protection
