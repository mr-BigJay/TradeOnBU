from __future__ import annotations

import pytest

from tests.db_safety import UnsafeTestDatabaseError, assert_destructive_db_allowed


def test_whitelist_accepts_primary_test_sqlite():
    assert_destructive_db_allowed("sqlite+aiosqlite:///./data/test_tradeonbu.db")


def test_whitelist_accepts_forward_e2e_sqlite():
    assert_destructive_db_allowed("sqlite+aiosqlite:///./data/test_forward_paper_e2e.db")


def test_whitelist_rejects_postgres():
    with pytest.raises(UnsafeTestDatabaseError):
        assert_destructive_db_allowed("postgresql://user:pass@staging-db.example/tradeonbu")


def test_whitelist_rejects_unknown_sqlite_file():
    with pytest.raises(UnsafeTestDatabaseError):
        assert_destructive_db_allowed("sqlite+aiosqlite:///./data/tradeonbu_prod.db")
