"""CLI utilities for installation and recovery."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from getpass import getpass

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import InstallationMeta, User
from app.services.passwords import hash_password, password_strength_ok
from app.services.session_auth import create_admin_user


def _read_password(args: argparse.Namespace, flag: str = "password") -> str:
    if getattr(args, "password_stdin", False):
        data = sys.stdin.read()
        if not data:
            raise SystemExit("No password on stdin")
        return data.rstrip("\n")
    if getattr(args, flag, None):
        raise SystemExit("Do not pass passwords on the command line; use --password-stdin")
    pw = getpass("New password: ")
    confirm = getpass("Confirm password: ")
    if pw != confirm:
        raise SystemExit("Passwords do not match")
    return pw


async def cmd_create_admin(username: str, password: str, domain: str | None) -> None:
    await init_db()
    async with SessionLocal() as session:
        ok, msg = password_strength_ok(password)
        if not ok:
            raise SystemExit(msg)
        existing = (await session.execute(select(User))).scalars().first()
        if existing:
            print("Administrator already exists — skipped")
        else:
            await create_admin_user(session, username, password)
        meta = (await session.execute(select(InstallationMeta).where(InstallationMeta.id == 1))).scalar_one_or_none()
        if meta is None:
            meta = InstallationMeta(id=1)
            session.add(meta)
        meta.installed = True
        meta.domain = domain
        meta.installed_at = datetime.now(timezone.utc)
        await session.commit()
    print("OK")


async def cmd_reset_password(username: str, password: str) -> None:
    await init_db()
    ok, msg = password_strength_ok(password)
    if not ok:
        raise SystemExit(msg)
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if not user:
            raise SystemExit("user not found")
        user.password_hash = hash_password(password)
        await session.commit()
    print("OK")


def main() -> None:
    parser = argparse.ArgumentParser(prog="tradeonbu-cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ca = sub.add_parser("create-admin")
    ca.add_argument("--username", required=True)
    ca.add_argument("--domain", default=None)
    ca.add_argument("--password-stdin", action="store_true", help="Read password from stdin (recommended)")

    rp = sub.add_parser("reset-password")
    rp.add_argument("--username", required=True)
    rp.add_argument("--password-stdin", action="store_true")

    args = parser.parse_args()
    if args.cmd == "create-admin":
        password = _read_password(args)
        asyncio.run(cmd_create_admin(args.username, password, args.domain))
    elif args.cmd == "reset-password":
        password = _read_password(args)
        asyncio.run(cmd_reset_password(args.username, password))


if __name__ == "__main__":
    main()
