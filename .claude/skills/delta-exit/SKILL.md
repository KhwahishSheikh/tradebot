---
name: delta-exit
description: Exit plan for open Delta positions - missing stop-loss, break-even, trailing stop, trend flip, funding cost. Use when the user asks about an open trade, "should I close", "where's my stop", or "protect my profit".
---

# delta-exit

Run `python3 exit.py` in `delta-assistant/` (all open positions, read-only) or offline:
`python3 exit.py SYMBOL buy|sell --entry X --size N [--stop S]`.

Rules: no stop -> structure stop first (24h swing + 0.25 ATR); >= 1R profit -> break-even option;
>= 2R -> trailing option (22h extreme -/+ 2 ATR); warn if the 1h trend turned against the position.
Suggested stops only ever tighten, never loosen an existing stop.

Show the options with what each locks in or risks, and the dry-run commands
(`python3 trade.py protect ...`, `python3 trade.py close ...`). The user chooses. Placing anything
follows delta-broker.
