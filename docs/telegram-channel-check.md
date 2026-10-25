# Temporary Telegram channel check

This standalone diagnostic checks membership and the content-protection flag
for BehDark (VIP), channel 1878414449. It does not install a listener, parse
signals, import application code, call OpenAI, or create any paper/live trades.
Protected-message extraction is not implemented. A successful check is not proof
of reliable signal ingestion; that requires a separate permitted feed and test.

Get your own api_id and api_hash from https://my.telegram.org under API development
tools. Enter them, your phone, the login code and optional two-step password only
in your own interactive SSH terminal. Never send these values in chat or put them
in command-line arguments. A bot token cannot replace these values.

Run with Python 3.12 and Telethon==1.45.0 in a disposable container, with this
script mounted read-only. No production directory, environment, database, or
Docker socket should be mounted. Run with `-it` rather than a shell heredoc so
hidden credential prompts can use the terminal.

The script uses MemorySession and disables update reception. Dialog discovery
necessarily receives last-message envelopes from Telegram, but the script does
not inspect or log message bodies or enumerate history. Only the target entity's
metadata is printed. It creates no session file, and logs out at completion.
If interrupted, killed, or logout fails, terminate the session named
`TradeOnBU temporary channel check` in Telegram Settings > Devices.

Share only the block between BEGIN SAFE REPORT and END SAFE REPORT. A protected
channel produces `content_protected: true`; this is a diagnostic result, not an
ingestion test. Coordinate a permitted integration/feed with the channel operator.

Local verification (no Telegram credentials or network required):

```sh
python3 -m unittest discover -s scripts -p test_telegram_channel_check.py
```
