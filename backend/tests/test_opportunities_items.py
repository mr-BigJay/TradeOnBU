from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import AiProposedSetup, SimPosition


@pytest.mark.asyncio
async def test_opportunities_items_pagination_wait_tab(session, client):
    now = datetime.now(timezone.utc)
    for i in range(25):
        session.add(
            AiProposedSetup(
                setup_id=f"wait-{i:03d}",
                run_id=1000 + i,
                symbol="BTCUSDT",
                direction="LONG",
                status="open",
                lifecycle_state="WAIT_LONG",
                setup_mode="conditional",
                created_at=now - timedelta(minutes=i),
                updated_at=now - timedelta(minutes=i),
            )
        )
    await session.commit()

    r1 = await client.get("/api/gui/opportunities/items?tab=wait&page=1")
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["total"] == 25
    assert body1["page_size"] == 20
    assert body1["total_pages"] == 2
    assert len(body1["items"]) == 20

    r2 = await client.get("/api/gui/opportunities/items?tab=wait&page=2")
    body2 = r2.json()
    assert len(body2["items"]) == 5
    assert body2["page"] == 2


@pytest.mark.asyncio
async def test_opportunities_pagination_over_200_setups_last_page(session, client):
    """Server-side count/limit must exceed legacy 200-row dashboard cap."""
    now = datetime.now(timezone.utc)
    n = 245
    for i in range(n):
        session.add(
            AiProposedSetup(
                setup_id=f"bulk-wait-{i:04d}",
                run_id=2000 + i,
                symbol="BTCUSDT",
                direction="LONG",
                status="open",
                lifecycle_state="WAIT_LONG",
                setup_mode="conditional",
                created_at=now - timedelta(seconds=i),
                updated_at=now - timedelta(seconds=i),
            )
        )
    await session.commit()

    first = await client.get("/api/gui/opportunities/items?tab=wait&page=1")
    assert first.status_code == 200
    body = first.json()
    assert body["total"] == n
    assert body["total_pages"] == 13

    last = await client.get("/api/gui/opportunities/items?tab=wait&page=13")
    last_body = last.json()
    assert last_body["page"] == 13
    assert len(last_body["items"]) == 5


@pytest.mark.asyncio
async def test_opportunities_expired_tab_over_50_and_last_page(session, client):
    now = datetime.now(timezone.utc)
    for i in range(55):
        session.add(
            AiProposedSetup(
                setup_id=f"exp-{i:03d}",
                run_id=3000 + i,
                symbol="BTCUSDT",
                direction="SHORT",
                status="expired",
                lifecycle_state="EXPIRED",
                setup_mode="conditional",
                created_at=now - timedelta(hours=i),
                updated_at=now - timedelta(hours=i),
            )
        )
    await session.commit()

    page1 = await client.get("/api/gui/opportunities/items?tab=expired&page=1")
    assert page1.json()["total"] == 55
    assert page1.json()["total_pages"] == 3

    page3 = await client.get("/api/gui/opportunities/items?tab=expired&page=3")
    assert page3.json()["page"] == 3
    assert len(page3.json()["items"]) == 15


@pytest.mark.asyncio
async def test_opportunities_other_tab_includes_null_lifecycle(session, client):
    session.add(
        AiProposedSetup(
            setup_id="null-lifecycle",
            run_id=4001,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state=None,
            setup_mode="conditional",
        )
    )
    session.add(
        AiProposedSetup(
            setup_id="pending-entry-other",
            run_id=4002,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state="PENDING_ENTRY",
            setup_mode="conditional",
        )
    )
    await session.commit()

    resp = await client.get("/api/gui/opportunities/items?tab=other&page=1")
    assert resp.status_code == 200
    ids = {row["setup_id"] for row in resp.json()["items"]}
    assert "null-lifecycle" in ids
    assert "pending-entry-other" in ids


@pytest.mark.asyncio
async def test_opportunities_summary_counts(session, client):
    session.add(
        AiProposedSetup(
            setup_id="ready-1",
            run_id=1,
            symbol="BTCUSDT",
            direction="LONG",
            status="open",
            lifecycle_state="READY_LONG",
            setup_mode="conditional",
        )
    )
    session.add(
        SimPosition(
            public_id="p-pend",
            symbol="BTCUSDT",
            direction="LONG",
            status="pending_entry",
            order_type="limit",
            margin_usdt=10,
            leverage=5,
            stop_loss=90,
            take_profit=110,
        )
    )
    await session.commit()

    summary = await client.get("/api/gui/opportunities/summary")
    assert summary.status_code == 200
    counts = summary.json()["counts"]
    assert counts["ready"] >= 1

    paper = await client.get("/api/gui/opportunities/items?tab=paper&paper_status=pending&page=1")
    assert paper.status_code == 200
    assert paper.json()["total"] >= 1
    assert paper.json()["items"][0]["entity_type"] == "paper_position"


@pytest.mark.asyncio
async def test_opportunities_items_invalid_tab(client):
    r = await client.get("/api/gui/opportunities/items?tab=not_a_tab")
    assert r.status_code == 400
