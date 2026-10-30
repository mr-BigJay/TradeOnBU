# Conditional Paper opportunity freshness

A forecast is not an execution. A level that already reacted must not silently
become a second-touch limit opportunity two days later.

## Behavior

New conditional plans (and structural revisions) carry `freshness_policy_version=1`
and their actual `registered_at_ms`. At the first monitor pass:

- Replay at most the preceding **one hour of closed 5m candles**, with another
  hour of contiguous swing context. Only bars closed at registration are eligible.
- Seed only an ordered structural prefix (`liquidity_sweep`, `mss`, `bos`,
  `breakout_close`). No AI confirmation flags are trusted. One event per candle;
  OHLC/time provenance is stored in `historical_confirmation_seed`.
- Never seed retests, pullbacks, entry touches or fills. Strategy B still requires
  its forward pullback/resumption; strategy C still requires a later retest.
- If the target or a later zone reaction already consumed the recovered structure,
  retire the plan. Incomplete history cannot grant a confirmation; forward
  monitoring continues and seeding stops once a live sequence starts.
- Existing BALANCED gates can place a pre-touch limit after all pre-entry
  requirements are met. STRICT remains STRICT. No retroactive fills are created.
- Before an order exists, reaching target retires the opportunity. A zone visit
  followed by a close outside the zone in the trade direction marks
  `first_touch_missed`, even if it happens in the same closed candle. This is a
  conservative missed-opportunity policy; it does not estimate profitability.
- For an already pending order, a proven target touch before any fill cancels it.
  A zone visit and favorable departure without reaching the limit also cancels
  the unfilled order. Earlier fills take precedence. Same-minute target/fill ambiguity or missing
  history suspends execution instead of inventing a fill or cancellation. This is
  a durable `pending_target_review_required` hold requiring operator review/cancel;
  it cannot silently resume after the target candle leaves the fetch window. Both
  monitors use the same reconciliation policy.
- Intraday conditional plans expire within **four hours** (shorter requested TTLs
  remain shorter). Repeated reports cannot extend a setup's expiry. These are
  explicit conservative defaults, not backtested optimal parameters.
- New-policy price changes archive and clear prior price tracking, then restart
  confirmation checks for the revised levels. A same-price sequence revision may
  preserve a compatible structural prefix under the existing merge rules.

Net R:R >= 2, fees, slippage, wallet limits, data-integrity checks and dry-run/shadow
controls remain enforced. No live exchange order code changes.

## Rollout

Rebuild after merging. No schema migration is required. Existing records do not
receive historical confirmations retroactively. The new policy applies on new
plan creation or structural revision. Existing pending/open trades retain their
original policy unless they already carry the version marker.

The investigated VPS was in **STRICT**, not BALANCED. To use pre-touch placement,
set Paper entry mode to `BALANCED`, shadow mode off and dry-run off through the
existing settings. Preserve the existing auto-Paper and wallet settings. The PR
does not silently switch production settings.

For a clean rollout, retire unexecuted legacy WAIT/READY/ICT-confirmed plans using
the operator procedure below, rather than allowing stale records to persist for
two more days. Pending/open positions are excluded. Stop the application during
this step to avoid a monitor writing a stale in-memory state. Back up the SQLite
DB using the SQLite backup API before any rollout mutation.

After a rebuild, while the `tradeonbu` service is stopped, run:

```bash
docker compose run --rm --no-deps -T tradeonbu python - <<'PY'
import asyncio
from datetime import datetime, timezone
from sqlalchemy import select
from app.db import SessionLocal
from app.models import AiProposedSetup, SimPosition
from app.services.runtime_settings import get_runtime_settings
from app.services.opportunity_freshness import retire
from app.services.opportunity_audit import log_setup_event

async def main():
    async with SessionLocal() as session:
        rt = await get_runtime_settings(session)
        rt.paper_entry_mode = 'BALANCED'
        rt.balanced_entry_shadow_mode = False
        rt.paper_entry_dry_run = False
        linked = select(SimPosition.ai_setup_id).where(SimPosition.ai_setup_id.is_not(None))
        rows = (await session.execute(select(AiProposedSetup).where(
            AiProposedSetup.status == 'open',
            AiProposedSetup.setup_mode == 'conditional',
            AiProposedSetup.lifecycle_state.in_(['WAIT_LONG', 'WAIT_SHORT', 'READY_LONG', 'READY_SHORT', 'ICT_CONFIRMED_LONG', 'ICT_CONFIRMED_SHORT']),
            AiProposedSetup.setup_id.not_in(linked),
        ))).scalars()
        count = 0
        for row in rows:
            if (row.conditional_plan or {}).get('freshness_policy_version') == 1:
                continue
            retire(row, 'legacy_plan_retired_for_reassessment', int(datetime.now(timezone.utc).timestamp()*1000))
            await log_setup_event(session, setup_id=row.setup_id, event_type='opportunity_retired', reason_code=row.last_rejection_reason)
            count += 1
        await session.commit()
        print({'mode': 'BALANCED', 'legacy_unexecuted_retired': count})
asyncio.run(main())
PY
```

Start the service again and check `/api/health`, then inspect new opportunities:
`historical_confirmation_seed`, `confirmation_state`, `unfilled_opportunity`,
`last_rejection_reason`, and linked `sim_positions`. Rollback code does not undo
retirements; new analyses must reassess opportunities. Never revive a historical
plan just because price later reached its target.
