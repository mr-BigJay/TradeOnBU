# TradeOnBU Notification Probe 0.1

Local-only Android feasibility test for a future notification-to-paper-trading relay.
This is **not** the relay or a trading client. It does not alter the backend, read
Telegram history, log in to Telegram, call an API or submit any order. It has no
INTERNET, SMS, contact or accessibility permission. Android 8+ (API 26), target 35.

## Test on the phone

1. Install the diagnostic APK. Keep the channel title exactly as shown in the phone's
   notification (default `BehDark (VIP)`).
2. Use the app's notification-access button and enable its listener in Android
   settings. OS access is broad; app code filters locally to the three official
   Telegram package names and the exact normalized channel title. Unsupported
   third-party clients produce no samples.
3. Press Start. Enable the target channel's notifications and previews in Telegram,
   leave the chat, and wait for a **new real notification**. No historical or currently
   active notification is imported when access is first granted.
4. Compare the captured raw text with the original post: symbol, side, full entry
   range, every TP and SL. Also test a later update/edit and the phone locked.
5. Copy the diagnostic report only if desired; review its private channel content
   before sharing. The report contains the latest three snapshots, not other chats.
   Stop capture / clear samples in the app, or revoke notification access to end.

## Data and limits

- Only matching, non-group-summary notifications are retained. Group summaries are
  ignored because they may combine conversations. Other Telegram notifications
  only increment a counter; their titles and content are not retained.
- Inspect `text`, `big_text`, `lines`, and MessagingStyle `messages`. Each field is
  capped at 2,000 characters, arrays at 8 elements, storage at 10 snapshots. Local
  app-private preferences are excluded from backup. Hashes deduplicate identical
  content for a notification key within the retained window.
- A notification snapshot is NOT a stable Telegram post ID. Bundled content can
  contain previous posts. Exact title matching is not cryptographic channel identity.
  This is insufficient by itself to safely auto-execute or manage edited signals.
- No hidden/redacted content is recovered; no copy-protection bypass is implemented.
  Content may be unavailable due to Telegram preview settings, protected messages,
  system redaction, aggregation, app state or vendor battery restrictions.
- Deletions, silent edits, missed notifications and media-only signals cannot be
  assumed available. Completeness always remains unknown until compared manually.
- No root is required. No server deployment or credentials are needed for this test.
- No AI calls or fees. Receipt on a real device has not been established by build tests.

## Build

Use JDK 17, Android API 35 and Build Tools 35.0.0 from the official Android SDK:

```bash
PROBE_ANDROID_JAR=/path/to/platforms/android-35/android.jar \
PROBE_BUILD_TOOLS=/path/to/build-tools/35.0.0 \
bash android/notification-probe/build.sh
```

If only a JRE is available, set `PROBE_ECJ_JAR` to Eclipse ECJ 3.40.0 from Maven
Central. The script runs eight pure-Java package/title-filter checks, compiles and
dexes the app, aligns and signs the APK, and verifies its signature.

Output: `build/TradeOnBU-Notification-Probe-0.1.apk`. The local generated key uses
an intentionally public test password and is for diagnostic builds only. Keep it
out of git. A rebuild with a different key requires uninstalling the prior test
app and loses its local samples. Do not reuse this signing configuration for a
production relay with credentials. APKs and keys are not committed.

## Gate before building the execution integration

A real sample must first demonstrate channel identity and complete fields. A relay
would additionally require explicit device pairing, HTTPS, scoped credentials,
persistent delivery/retry, replay resistance, message/update identity, freshness,
fail-closed parsing, duplicate prevention and separate PAPER-only risk limits.
Until that is validated, incomplete or ambiguous notifications cannot become orders.
