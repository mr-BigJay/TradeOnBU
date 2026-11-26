"""One-shot Telegram membership/protection check; no application or trading imports."""
from __future__ import annotations

import asyncio
import getpass
import json
import logging
import re
import sys

CHANNEL_ID = 1878414449
EXPECTED_TITLE = "BehDark (VIP)"


def channel_report(entity) -> dict:
    if entity.id != CHANNEL_ID:
        raise ValueError("Unexpected channel")
    return {
        "channel_id": entity.id,
        "title_matches": entity.title == EXPECTED_TITLE,
        "member": not bool(getattr(entity, "left", False)),
        "broadcast": bool(getattr(entity, "broadcast", False)),
        "content_protected": bool(getattr(entity, "noforwards", False)),
        "message_content_tested": False,
        "server_listener_installed": False,
        "trades_created": 0,
        "openai_requests": 0,
    }


async def inspect_channel(client) -> dict:
    # Dialog listing includes Telegram's last-message envelope. Do not inspect,
    # print, persist, forward or download its content, or enumerate chat history.
    async for dialog in client.iter_dialogs():
        if dialog.is_channel and dialog.entity.id == CHANNEL_ID:
            # Refresh the flags rather than relying only on cached dialog data.
            entity = await client.get_entity(dialog.input_entity)
            return channel_report(entity)
    return {"channel_found": False, "message_content_tested": False}


async def run(api_id: int, api_hash: str, phone: str) -> int:
    from telethon import TelegramClient, errors
    from telethon.sessions import MemorySession

    # Never reuse or export the web browser's session. No .session file is made.
    client = TelegramClient(
        MemorySession(), api_id, api_hash,
        device_model="TradeOnBU temporary channel check",
        receive_updates=False, request_retries=0, connection_retries=1,
        flood_sleep_threshold=0, timeout=20,
    )
    authenticated = False
    try:
        await asyncio.wait_for(client.connect(), timeout=40)
        sent = await asyncio.wait_for(client.send_code_request(phone), timeout=40)
        code = getpass.getpass("Telegram login code (hidden): ").strip()
        try:
            await asyncio.wait_for(client.sign_in(
                phone=phone, code=code, phone_code_hash=sent.phone_code_hash,
            ), timeout=40)
        except errors.SessionPasswordNeededError:
            password = getpass.getpass("Telegram two-step password (hidden): ")
            await asyncio.wait_for(client.sign_in(password=password), timeout=40)
        authenticated = True
        print("\nBEGIN SAFE REPORT")
        report = await asyncio.wait_for(inspect_channel(client), timeout=120)
        print(json.dumps(report, indent=2))
        print("END SAFE REPORT")
        if report.get("content_protected"):
            print("Protected content detected. No signal ingestion attempted.")
        return 0
    except errors.FloodWaitError as exc:
        print(f"Telegram rate limit: wait {exc.seconds} seconds. Do not retry now.")
        return 2
    except (errors.RPCError, OSError, asyncio.TimeoutError, ValueError) as exc:
        # Class names only: RPC exceptions may embed private request details.
        print(f"Check failed: {type(exc).__name__}")
        return 2
    finally:
        try:
            if authenticated:
                try:
                    revoked = await asyncio.wait_for(client.log_out(), timeout=20)
                    print("Temporary session revoked." if revoked else
                          "Logout unconfirmed: terminate the temporary check session in Telegram Settings > Devices.")
                except Exception:
                    print("Logout unconfirmed: terminate the temporary check session in Telegram Settings > Devices.")
            else:
                print("If a temporary check session appears in Telegram Settings > Devices, terminate it.")
        finally:
            await client.disconnect()


def main() -> int:
    if not sys.stdin.isatty():
        print("Run interactively in your SSH terminal; do not pipe credentials.")
        return 2
    logging.disable(logging.CRITICAL)
    print("Read-only channel metadata check. Credentials are entered only here.")
    try:
        api_id_text = getpass.getpass("Telegram api_id (hidden): ").strip()
        api_hash = getpass.getpass("Telegram api_hash (hidden): ").strip()
        phone = getpass.getpass("Phone with country code (hidden): ").strip()
        if not api_id_text.isdigit() or int(api_id_text) <= 0:
            raise ValueError("api_id")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
            raise ValueError("api_hash")
        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
            raise ValueError("phone")
        return asyncio.run(run(int(api_id_text), api_hash, phone))
    except (ValueError, EOFError):
        print("Invalid or missing input. Nothing was saved.")
        return 2
    except KeyboardInterrupt:
        print("Cancelled. Check Telegram Settings > Devices for the temporary session.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
