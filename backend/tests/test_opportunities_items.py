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
