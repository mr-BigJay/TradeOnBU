"""Hard guardrails before destructive test database operations."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlparse

ALLOWED_SQLITE_FILENAMES = frozenset(
    {
        "test_tradeonbu.db",
        "test_forward_paper_e2e.db",
        ":memory:",
    }
)

FORBIDDEN_URL_SUBSTRINGS = (
    "production",
    "staging",
    "/prod",
    "prod.db",
    "postgres",
    "postgresql",
    "mysql",
    "mariadb",
    "bitunix",
    "rds.amazonaws",
    "supabase",
)


class UnsafeTestDatabaseError(RuntimeError):
    """Raised when DATABASE_URL is not an approved isolated test target."""


def _sqlite_file_from_url(database_url: str) -> str | None:
    parsed = urlparse(database_url)
    if parsed.scheme not in ("sqlite", "sqlite+aiosqlite"):
        return None
    path = unquote(parsed.path or "")
    if path in (":memory:", "/:memory:"):
        return ":memory:"
    if path.startswith("/"):
        return Path(path).name
    if path.startswith("./"):
        return Path(path[2:]).name
    return Path(path).name


def assert_destructive_db_allowed(database_url: str) -> None:
    """
    Abort pytest before drop_all/create_all unless URL is a known local test SQLite file.
    """
    if not database_url or not str(database_url).strip():
        raise UnsafeTestDatabaseError("DATABASE_URL is empty")

    lowered = database_url.lower()
    for token in FORBIDDEN_URL_SUBSTRINGS:
        if token in lowered:
            raise UnsafeTestDatabaseError(f"DATABASE_URL contains forbidden token {token!r}: {database_url!r}")

    filename = _sqlite_file_from_url(database_url)
    if filename is None:
        raise UnsafeTestDatabaseError(
            f"Only sqlite test files are allowed for destructive fixtures, got: {database_url!r}"
        )

    if filename not in ALLOWED_SQLITE_FILENAMES:
        raise UnsafeTestDatabaseError(
            f"SQLite file {filename!r} is not whitelisted; allowed: {sorted(ALLOWED_SQLITE_FILENAMES)}"
        )

    if filename != ":memory:":
        raw_path = database_url.split("///", 1)[-1]
        resolved = Path(unquote(raw_path)).expanduser().resolve()
        if "test" not in resolved.name.lower():
            raise UnsafeTestDatabaseError(f"Database path must be a test file, got {resolved}")
