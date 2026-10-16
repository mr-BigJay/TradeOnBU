"""Migration smoke test on a disposable pre-monitor SQLite schema.

The test never opens a production or staging database. It catches the
regression where startup re-migrates modern WAIT_* setups into READY_*.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.db_safety import assert_destructive_db_allowed


@pytest.mark.asyncio
async def test_legacy_upgrade_preserves_rows_and_modern_wait_is_not_rewritten(monkeypatch):
    import app.db as db

    # Isolated per-test SQLite engine, with no production or staging data.
    database_url = "sqlite+aiosqlite:///:memory:"
    assert_destructive_db_allowed(database_url)
    engine = create_async_engine(database_url)
    monkeypatch.setattr(db, "engine", engine)
    try:
        # Deliberately represent the original table *before* the new monitor
        # fields were added by the PR. Other tables are irrelevant to this case.
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE ai_proposed_setups ("
                "id INTEGER PRIMARY KEY, setup_id VARCHAR(36), "
                "status VARCHAR(32), direction VARCHAR(16), entry_price FLOAT)"
            ))
            await conn.execute(text(
                "INSERT INTO ai_proposed_setups "
                "(id, setup_id, status, direction, entry_price) VALUES "
                "(1, 'old-long', 'open', 'LONG', 100), "
                "(2, 'old-short', 'open', 'SHORT', 200), "
                "(3, 'old-inv', 'invalidated', 'LONG', 300), "
                "(4, 'old-exp', 'expired', 'SHORT', 400)"
            ))

        await db.migrate_db()

        async with engine.begin() as conn:
            columns = {
                row[1] for row in (await conn.execute(text(
                    "PRAGMA table_info(ai_proposed_setups)"
                ))).all()
            }
            assert {
                "lifecycle_state", "setup_mode", "monitor_from_ms",
                "monitoring_enabled", "final_ai_decision", "monitor_cursor_ms",
            } <= columns
            rows = (await conn.execute(text(
                "SELECT id, setup_id, status, direction, entry_price, "
                "lifecycle_state, setup_mode, monitoring_enabled "
                "FROM ai_proposed_setups ORDER BY id"
            ))).mappings().all()
            assert [r["setup_id"] for r in rows] == [
                "old-long", "old-short", "old-inv", "old-exp"
            ]
            assert [r["lifecycle_state"] for r in rows] == [
                "READY_LONG", "READY_SHORT", "INVALIDATED", "EXPIRED"
            ]
            assert [r["entry_price"] for r in rows] == [100, 200, 300, 400]
            assert all(r["setup_mode"] == "legacy_immediate" for r in rows)
            assert all(r["monitoring_enabled"] == 0 for r in rows)

            # Modern conditional setups are valid WAIT_* rows with a real
            # final_ai_decision and monitor start. Never auto-promote these.
            await conn.execute(text(
                "INSERT INTO ai_proposed_setups "
                "(id, setup_id, status, direction, entry_price, "
                "lifecycle_state, setup_mode, monitoring_enabled, "
                "monitor_from_ms, final_ai_decision) VALUES "
                "(5, 'new-wait', 'open', 'LONG', 500, 'WAIT_LONG', "
                "'conditional', 1, 300000, 'WAIT')"
            ))

        # Running init-time migration repeatedly must be harmless.
        await db.migrate_db()
        await db.migrate_db()
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT lifecycle_state, setup_mode, monitoring_enabled, "
                "monitor_from_ms, final_ai_decision "
                "FROM ai_proposed_setups WHERE setup_id = 'new-wait'"
            ))).mappings().one()
            assert dict(row) == {
                "lifecycle_state": "WAIT_LONG",
                "setup_mode": "conditional",
                "monitoring_enabled": 1,
                "monitor_from_ms": 300000,
                "final_ai_decision": "WAIT",
            }
            old = (await conn.execute(text(
                "SELECT lifecycle_state, setup_mode, monitoring_enabled "
                "FROM ai_proposed_setups WHERE setup_id = 'old-long'"
            ))).mappings().one()
            assert dict(old) == {
                "lifecycle_state": "READY_LONG",
                "setup_mode": "legacy_immediate",
                "monitoring_enabled": 0,
            }
            count = (await conn.execute(text(
                "SELECT COUNT(*) FROM ai_proposed_setups"
            ))).scalar_one()
            assert count == 5
    finally:
        await engine.dispose()
