# Bitunix Futures — initial read-only connectivity check

**Safety:** This is a one-shot interactive diagnostic tool, NOT a live execution integration. The app remains paper-only. The private client is restricted to three GET endpoints and has no order-writing methods.

1. Generate a dedicated Bitunix API key with IP whitelist set to the server's outbound IP. Never enable withdrawal permission. If any part of the key or secret was shared in a screenshot, revoke and replace the key first.
2. Do not paste keys into ChatGPT, GitHub, shell command arguments, .env, or logs.
3. After merging and deploying this branch into the app container, run from a live SSH terminal **without -T**:

   ```bash
   cd /opt/tradeonbu
   docker compose exec tradeonbu python -m app.bitunix_readonly_probe
   ```

4. Paste each key at its prompt (input echo is disabled). Credentials remain in process memory for the three signed GET requests; they are not saved by the probe. Successful output is `BITUNIX_READ_ONLY_OK` with position mode and counts, but no keys, balances, account IDs, or individual position details.
5. If it fails, do **not** enable live trading or place test orders. Check permissions, outbound IP whitelist, and server time; report only the sanitized error.

Signed requests follow the [official Bitunix Futures REST signature specification](https://www.bitunix.com/api-docs/futures/common/sign.html): SHA256(SHA256(nonce + timestamp + api-key + sorted-key/value query string + body) + secret). Each request uses a fresh random 64-bit nonce and current millisecond timestamp; only HTTPS `https://fapi.bitunix.com` is used.

Verified official read endpoints:
- https://www.bitunix.com/api-docs/futures/account/get_single_account.html
- https://www.bitunix.com/api-docs/futures/position/get_pending_positions.html
- https://www.bitunix.com/api-docs/futures/trade/get_pending_orders.html
