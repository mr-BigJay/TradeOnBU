# Structure A observation experiment

Set ICT_STRUCTURE_SHADOW=1 on an experimental instance to enable one-minute
observation ticks and the secondary review in the existing hourly inference.
Default is off. The observer only writes ict_structure_observations.
It creates no SimPosition, setup or wallet entry and sends no Telegram message.

The swing variant requires two confirmed highs and lows establishing the opposite
trend, a reclaiming liquidity sweep, a later close through the referenced opposing
pivot with directional body at least 55% of its range, and a directional three-bar
FVG at the break or the next candle. A pivot needs two candles on each side.
It compares against the existing 12/8-bar MSS/BOS definitions, using identical
FVG selection, stop, 2.5 gross cap, net 2R gate and 5m/15m obstacle checking.
This phase covers FVG entries. It does not introduce Order Block detection.

Both variants observe the first touch, wait for the closed 5m confirmation and
then simulate a future midpoint limit. SL is beyond the sweep by 0.1 USD.
First touch before observation is marked late. A touch in the registration
minute is ambiguous. Lifetime is one hour from FVG close. These are
experiment choices, not optimizations based on replay PnL.
Costs are frozen at observation from Paper settings. Entry/TP use requested
prices, SL uses adverse configured slippage and gaps. Ambiguous TP/SL ordering
and data gaps terminate with no reported profit. Open observations remain marked
open. Hypotheses may overlap; summed hypothesis PnL is not a portfolio return.

The secondary AI review receives immutable keys and evidence in the existing
hourly request. It adds tokens but no extra API call. Its response is recorded
with the completed run time and does not authorize orders. It is observational.
There is no retroactive filtering of already observed touches. Supplying the extra
context can influence a model response, so evaluate in the experimental instance first.
The ordinary prompt is unchanged when the feature is off.
The journal appears under Opportunities > structure experiment.
Enable only one application scheduler against this experiment database.

Inspection/test commands:
    cd backend
    python -m pytest tests/test_structure_shadow.py -q
