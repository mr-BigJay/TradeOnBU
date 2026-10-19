# AI Entry Confirmation & Execution Engine (STRICT vs BALANCED)

## Root causes (verified in code)

1. **One ICT token per closed 5m bar** — `process_bar_sequential()` in `ict_confirmation_engine.py` advances at most one token per bar. Multiple signals inside the same 5m window are dropped until later bars (often too late for entry).
2. **Closed-bar latency** — `_eligible_closed_bars()` requires `now_ms >= candle_close_ms(bar, "5m")`, so intra-bar POI touches are invisible until the 5m close (+ monitor tick interval, default ~60s).
3. **Limit orders after touch** — promotion to `READY_*` required full sequence including `entry_zone_touch` before `_attempt_ready_paper_order()`, so limit creation could lag the first zone touch by up to one 5m close and one monitor pass.
4. **Redundant re-gating** — `objective_confirmations_satisfied()` re-validates the full ICT chain at order time (appropriate for STRICT); BALANCED adds a pre-entry path when structural tokens are proven without re-analyzing AI direction.
5. **1m chronology existed only in replay** — `confirmation_replay.chronology_provable_on_1m()` was used for miss audit, not live monitoring.

## Architecture

| Mode | Confirmation driver | Order timing |
|------|---------------------|--------------|
| **STRICT** (default) | `process_bar_sequential` on closed 5m only | After full sequence + touch evidence |
| **BALANCED** | `advance_balanced_on_closed_5m_bar` (closed 1m inside each 5m + 5m fallback) | After full sequence **or** pre-entry structural tokens (limit staged before touch) |
| **Shadow** | Same as BALANCED | No `maybe_auto_open_paper_position`; timeline `balanced_shadow_skip_order` |

Pipeline metadata on `objective_notes.execution_pipeline`: `AI_VALIDATED` → `ENTRY_ELIGIBLE` → `ORDER_PENDING` → `POSITION_OPEN`.

Risk invariants unchanged: `HARD_MIN_AUTO_PAPER_NET_RR`, VIP1 fees, spread/slippage, paper-only, TP cap, max positions, no look-ahead (only closed 1m/5m).

## Settings

Dashboard → Strategy: **Paper entry mode** (`STRICT` | `BALANCED`) and **Shadow mode** (BALANCED only).

## Tests

- `backend/tests/test_balanced_entry_execution.py` — 1m multi-token vs STRICT, pre-entry objective, shadow skip.
- Existing STRICT regression suite must remain green (`pytest backend/tests`).

## Limitations

- BALANCED still requires approved ICT profiles from `conditional_setup.CONFIRMATION_PROFILES` (no arbitrary token bags).
- Pre-entry limits rely on `monitor_from_ms` / order `monitor_from_ms` for fill policy (no retroactive fill before order).
- PR #22 (`cursor/paper-execution-engine-2ec7`) market/limit selection and daily caps are **not** merged here; integrate after this PR lands.
