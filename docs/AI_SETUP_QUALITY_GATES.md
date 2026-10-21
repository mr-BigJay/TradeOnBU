# AI setup quality gates (paper ICT)

Server-side checks in `ai_setup_quality_gates.py` run at setup persistence (no extra ChatGPT calls).

- **Immediate LONG/SHORT:** all four `*_clear` booleans must be true.
- **Conditional WAIT:** initial `*_clear=false` is allowed; counter-trend WAIT is not blocked by AI boolean `contrarian_ict_evidence` at persist time.
- **Repeat same direction after paper loss (`sl_hit`, negative PnL, etc.):** immediate entries require objective ICT sequence hits on **closed 5m bars after** the loss timestamp; WAIT defers proof to the monitor.
- **Parallel open same direction:** new row rejected — use conditional revision on the existing setup.
- **Before paper order (READY):** re-check structural invalidation on latest closed 5m, confirmations strictly after prior same-direction loss, and no stacked open/pending sim position in the same direction (including manual sim rows with `ai_setup_id` NULL). Uses the opportunity monitor’s verified 5m bundle — fail-closed when data is incomplete or unverified (no extra fetch).
- **Before limit fill (PENDING):** same execution gates on **event-time** closed 5m (as of the 1m fill bar), plus setup expiry. Does not re-block valid ICT reversals using stale AI `market_regime`; regime is not an execution gate.
- **Stale `market_intelligence`:** blocks high/medium `market_regime_confidence` without `market_context_caution_ack`.

Conditional revisions run the same persist gates and append `quality_audit_history` without removing prior revision history.

Audit: `setup_quality_audit` on structured snapshot and `objective_notes.setup_quality_audit`.
