---
name: delta-timemachine
description: Replay a Delta trade idea over real history and 10,000 Monte Carlo paths to see how often the target vs stop is hit first. Use when the user asks "would this have worked", "what are the odds", or wants to test a stop/target.
---

# delta-timemachine

Run in `delta-assistant/`:
`python3 timemachine.py SYMBOL buy|sell --stop-pct 0.6 --target-pct 1.2 [--hours 24] [--days 60] [--sims 10000]`
or `--entry E --stop S --target T`.

It prints two views: REPLAY (enter at every past 15m candle with the same % stop/target) and MONTE CARLO
(10,000 paths stitched from random 8-candle blocks of real history). Same-candle stop+target counts as
stop. Report target-first %, stop-first %, still-open %, and average R after fees.

Always say: simulation on past data, not a track record and not a prediction. A negative average R
means the stop/target pair lost money historically — say that plainly.
