"""SQLite-backed lock lease behavior (matches production docker-compose DB)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.db import SessionLocal, init_db
from app.services.opportunity_monitor_lock import (
    _LOCK_TTL_SECONDS,
    new_monitor_owner,
    refresh_opportunity_monitor_lock,
    release_opportunity_monitor_lock,
    try_acquire_opportunity_monitor_lock,
)


@pytest.mark.asyncio
async def test_lock_refresh_prevents_takeover_during_long_tick():
    await init_db()
    owner1 = new_monitor_owner()
    owner2 = new_monitor_owner()
    base = 1_700_000_000_000
    with patch("app.services.opportunity_monitor_lock._now_ms") as mock_ms:
        mock_ms.return_value = base
        async with SessionLocal() as s1:
            assert await try_acquire_opportunity_monitor_lock(s1, owner1)
            await s1.commit()
        mock_ms.return_value = base + 20_000
        async with SessionLocal() as s1:
            assert await refresh_opportunity_monitor_lock(s1, owner1)
        mock_ms.return_value = base + (_LOCK_TTL_SECONDS * 1000) - 5_000
        async with SessionLocal() as s2:
            assert await try_acquire_opportunity_monitor_lock(s2, owner2) is False

    async with SessionLocal() as s1:
        await release_opportunity_monitor_lock(s1, owner1)
    with patch("app.services.opportunity_monitor_lock._now_ms", return_value=base + 120_000):
        async with SessionLocal() as s2:
            assert await try_acquire_opportunity_monitor_lock(s2, owner2)


@pytest.mark.asyncio
async def test_expired_lock_without_refresh_allows_recovery():
    await init_db()
    owner1 = new_monitor_owner()
    owner2 = new_monitor_owner()
    base = 1_700_000_000_000
    with patch("app.services.opportunity_monitor_lock._now_ms") as mock_ms:
        mock_ms.return_value = base
        async with SessionLocal() as s1:
            assert await try_acquire_opportunity_monitor_lock(s1, owner1)
            await s1.commit()
        mock_ms.return_value = base + (_LOCK_TTL_SECONDS + 10) * 1000
        async with SessionLocal() as s2:
            assert await try_acquire_opportunity_monitor_lock(s2, owner2)
