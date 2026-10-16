#!/usr/bin/env python3
"""Verify PR19 migration on a local SQLite snapshot, never the live database.

Usage in isolated docker with /app/data/live-backup.db (untouched snapshot)
and /app/data/test_tradeonbu.db (writable disposable migration target).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

SOURCE = Path("/app/data/live-backup.db")
TARGET = Path("/app/data/test_tradeonbu.db")


def inspect(path: Path) -> tuple[dict[str, int], str]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        counts = {}
        for name in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            counts[name] = int(con.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
        return counts, integrity


def setup_state(path: Path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
        if not con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='ai_proposed_setups' AND type='table'"
        ).fetchone():
            return []
        return con.execute(
            "SELECT id, lifecycle_state, setup_mode, monitoring_enabled, "
            "monitor_from_ms, final_ai_decision FROM ai_proposed_setups ORDER BY id"
        ).fetchall()


async def main() -> None:
    import app.db as db

    assert SOURCE.is_file() and TARGET.is_file()
    assert SOURCE.resolve() != TARGET.resolve()
    assert db.settings.database_url == "sqlite+aiosqlite:////app/data/test_tradeonbu.db", (
        "Refusing to use unexpected database URL"
    )
    old_counts, old_integrity = inspect(SOURCE)
    assert old_integrity == "ok", f"Original snapshot has SQLite integrity issue: {old_integrity}"
    print(f"Snapshot verified: {len(old_counts)} existing tables", flush=True)

    await db.init_db()
    new_counts, integrity = inspect(TARGET)
    assert integrity == "ok", f"Migrated database integrity check failed: {integrity}"
    for table, old_count in old_counts.items():
        assert table in new_counts, f"Migration removed existing table {table}"
        assert new_counts[table] == old_count, (
            f"Row count changed for {table}: {old_count} -> {new_counts[table]}"
        )

    with sqlite3.connect(f"file:{TARGET}?mode=ro", uri=True) as con:
        for statement in db.SIGNAL_MIGRATIONS:
            # Legacy ALTER migrations must result in all listed columns.
            words = statement.split()
            table, column = words[2], words[5]
            assert all(t.replace("_", "").isalnum() for t in (table, column))
            cols = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
            assert column in cols, f"Migration did not produce {table}.{column}"

    first = setup_state(TARGET)
    await db.init_db()
    second = setup_state(TARGET)
    assert first == second, "Startup migration is not idempotent for setup status"
    after_counts, integrity2 = inspect(TARGET)
    assert integrity2 == "ok"
    assert after_counts == new_counts, "Repeated startup unexpectedly changed row counts"
    print(
        "MIGRATION_OK: original table counts preserved, required columns present, "
        "repeated startup idempotent, SQLite integrity OK",
        flush=True,
    )
    print("Live DB was never mounted inside the test container.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
