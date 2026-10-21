# AI setup quality gates (paper ICT)

Server-side checks in `ai_setup_quality_gates.py` run at setup persistence (no extra ChatGPT calls).

- **Immediate LONG/SHORT:** all four `*_clear` booleans must be true.
- **Counter-trend / regime fade:** `contrarian_ict_evidence` (sweep + MSS/BOS + POI) when `trade_against_dominant_trend` or regime opposes direction.
- **Repeat same direction after paper loss:** requires `distinct_new_setup` + `setup_independence` prose.
- **Parallel open same direction:** same independence requirement for a second row.
- **Stale `market_intelligence`:** blocks high/medium `market_regime_confidence` without `market_context_caution_ack`.

Conditional WAIT rows may keep initial `*_clear=false`; the opportunity monitor records objective confirmations before paper orders.

Audit: `setup_quality_audit` on structured snapshot and `objective_notes.setup_quality_audit`.
