# Cap automatic paper position TP to 3R (new positions only)

TradeOnBU preserves the AI ICT analysis, the suggested entry, stop loss,
take-profit level and persisted `AiProposedSetup` without modification.

For every **new automatically created paper limit order**, calculate the gross
price-distance ratio based on *planned entry* and the original structural SL:

`R = abs(entry - SL)`

- LONG: `execution_TP = min(AI_TP, entry + 3 * R)`
- SHORT: `execution_TP = max(AI_TP, entry - 3 * R)`
- 2R, 2.5R and 3R stay unchanged; 3.1R, 3.4R and 5R all execute at 3R.
- This is a gross R:R cap, **not** a fixed multiplier of dollars earned and
  **not** a change to the model prompt, AI forecasts, fee calculations or SL.
- No order is refused merely because the original analytical target exceeds 3R.

The cap is applied in `maybe_auto_open_paper_position()`, just before creating
an `OrderRequest`. Only the new `SimPosition.take_profit` receives the
shortened TP. Historical, open and pending orders already in the database are
not changed, and manual position orders are not affected.

The existing hard minimum net R:R safety check still uses the **actual
executable TP** after cap, including paper execution costs. Orders that fail
an existing safety rule continue to be rejected for that independent reason.

For auditability, the new `SimPosition.extra["tp_execution_policy"]` records:
AI original TP, executable TP, whether the cap was applied, maximum gross R:R,
original gross R:R, and executable gross R:R. Both targets are shown in the
position card and are returned with closed trade history. The AI setup's
`take_profit` and original ICT entry snapshot stay untouched.

This version does not add a database migration or modify any existing position.
