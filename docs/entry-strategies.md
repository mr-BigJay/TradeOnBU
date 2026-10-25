# Shared AI entry strategies

A (`ict_reversal`) preserves existing ICT behavior and profiles.
B (`trend_continuation`) selects an aligned 4H/1H trend pullback. Its objective
sequence is entry-zone pullback, directional close beyond the previous execution
candle high/low, then entry-zone touch.
C (`breakout_retest`) supplies a fixed `breakout_level` inside the entry zone.
Its sequence is a close crossing that level, a later candle touching the level and
closing on the breakout side, then entry-zone touch. A wick-only break is insufficient.
B/C do not require reversal sweeps or FVGs. Both persist as conditional WAIT.
Confirmation starts after monitoring begins; earlier breakouts do not count.
STRICT uses closed 5m bars. BALANCED uses actual closed 1m bars for new tokens,
not accumulated partial-5m wicks. Existing chronology/integrity checks remain.

All three default to enabled. AI analysis page switches use admin-only PUT
`/api/gui/chatgpt/strategies/settings`; GET `/api/gui/chatgpt/strategies` reads them.
At least one stays enabled. Existing SettingEntry storage and audit log are used;
no migration. Disabling B/C restores the exact old A prompt. Legacy snapshots
without strategy_id retain A semantics.

One shared inference evaluates enabled strategies and selects at most one setup.
No extra AI calls, schedule/model/reasoning changes. Actual token usage and the
switch snapshot are retained per run and visible in detail, not a cost guarantee.
Daily outlook is unchanged. AI judges higher-timeframe trend and price structure;
schema checks cannot independently prove those judgments. Backend checks alignment
for B, finite ordered levels, structural stop/invalidation and gross R:R >= 2 at
the worst entry-zone edge. Existing net R:R, fees, position/loss/duplicate and fill
safeguards still apply. Setup and auto paper position snapshots retain strategy ID.
A setup cannot change strategy through revision. Changing C's breakout level resets
confirmations even for small price changes.

Switches are checked at candidate persistence, order creation and AI-linked paper
fill. Disabling blocks new entries; pending orders cancel when their fill is next
evaluated. Monitoring can continue; enabling can resume an unexpired WAIT setup.
Open positions continue being managed. An in-flight report can show a candidate
whose strategy was disabled during inference; persistence rejects that candidate.

Validation uses mocked AI and offline candles. Profitability and live-provider
output quality are not established. Compare actual usage and paper results after
deploy. No paid inference, production deployment or real order was performed.

Regression note: `test_record_run_outcome_sets_future_next_attempt` also fails on
the unchanged base: it expects completion-relative +59 minutes, while the shipped
scheduler uses fixed UTC boundaries plus one minute. This change does not edit the
scheduler or that test.
