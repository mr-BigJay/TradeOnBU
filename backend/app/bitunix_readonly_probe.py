"""Interactive one-shot Bitunix auth check. No credentials saved; no trade API calls.

Run from the backend container with an attached interactive terminal:
    docker compose exec tradeonbu python -m app.bitunix_readonly_probe
"""

from __future__ import annotations

import asyncio
from getpass import getpass

from app.services.bitunix_private_readonly import BitunixReadOnlyClient, BitunixReadOnlyError


async def _run(api_key: str, secret_key: str) -> None:
    api = BitunixReadOnlyClient(api_key=api_key, secret_key=secret_key)
    account = await api.account_usdt()
    positions = await api.open_btc_positions()
    orders = await api.open_btc_orders()
    print("BITUNIX_READ_ONLY_OK")
    print("USDT_ACCOUNT_FOUND: yes")
    print("POSITION_MODE:", account.get("positionMode", "unknown"))
    print("OPEN_BTC_POSITIONS:", len(positions))
    print("PENDING_BTC_ORDERS:", len(orders))
    print("No orders were submitted, modified or canceled.")


def main() -> int:
    print("TradeOnBU - Bitunix Futures read-only connection check")
    print("Credentials are hidden while typing and NOT saved to disk.")
    try:
        api_key = getpass("Bitunix API Key (hidden): ")
        secret_key = getpass("Bitunix Secret Key (hidden): ")
        asyncio.run(_run(api_key, secret_key))
        return 0
    except (BitunixReadOnlyError, ValueError) as exc:
        print("BITUNIX_READ_ONLY_FAILED:", exc)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Canceled")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
