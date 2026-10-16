from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.setup_lifecycle import READY_LONG
from app.models import AiProposedSetup


def ready_long_setup(**kwargs) -> AiProposedSetup:
    now = datetime.now(timezone.utc)
    defaults = dict(
        setup_id="setup-ready",
        run_id=1,
        symbol="BTCUSDT",
        direction="LONG",
        status="open",
        lifecycle_state=READY_LONG,
        entry_price=100.0,
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        stop_loss=94.0,
        take_profit=115.0,
        expires_at=now + timedelta(hours=24),
        paper_fill_recorded=False,
        structured_snapshot={
            "decision": "LONG",
            "liquidity_event_clear": True,
            "poi_reaction_clear": True,
            "structural_confirmation_clear": True,
            "logical_invalidation_clear": True,
        },
        confirmation_state={
            "sequence": ["entry_zone_touch"],
            "completed": ["entry_zone_touch"],
            "missing": [],
            "confirmation_timestamps": {"entry_zone_touch": 1_000},
            "confirmation_evidence": {
                "entry_zone_touch": {
                    "bar_low": 99.5,
                    "bar_high": 100.5,
                    "zone_lo": 99.0,
                    "zone_hi": 101.0,
                }
            },
        },
    )
    defaults.update(kwargs)
    return AiProposedSetup(**defaults)
