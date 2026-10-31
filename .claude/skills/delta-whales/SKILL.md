---
name: delta-whales
description: What big players are doing on Delta - large trades right now, 5-minute volume bursts, open-interest jumps, funding shifts. Use when the user asks about whales, big money, smart money, "who is buying", or why price suddenly moved.
---

# delta-whales

Run `python3 whales.py [SYMBOLS] [--hours 24] [--min-usd 50000]` in `delta-assistant/` (public data).

Summarise per symbol in 3-5 bullets:
- Big trades now (Delta only publishes the last ~50 trades, so this covers seconds to minutes) and
  whether aggressive buyers or sellers dominate.
- Volume bursts: when big money hit the market and which way price went.
- OI jumps: big positions opening or closing (new longs, new shorts, covering, liquidations).
- Funding shifts: which side has been crowding in.

Always add: big players can be wrong, hedging, or market makers — this is not a signal to copy.
Not financial advice.
