---
name: delta-entry
description: Check whether the written Delta strategy rules (trend pullback, London opening-range breakout) give an entry now, with entry/stop/target/size. Use when the user asks "should I enter", "any setup", "entry for BTC".
---

# delta-entry

Run `python3 entry.py [SYMBOLS]` in `delta-assistant/` (reads the account read-only via .env keys;
or `--balance N` offline). The exact rules are in the docstring at the top of `entry.py`:

- **Trend pullback**: 4h and 1h trend agree, 15m pulls back to EMA20 and turns, RSI in range,
  stop beyond the 12-bar 15m extreme (>= 1 ATR), target 2R.
- **London opening-range breakout**: 09:00-12:00 UK weekdays, break of the 08:00-09:00 UK range with
  volume, not against the 4h trend, stop at range middle, target 2R.

Every setup is passed through the risk check (delta-risk). Present: strategy, side, entry, stop, target,
R:R, contracts, $ at risk and % of balance, liquidation estimate, verdict, and the `trade.py` ticket line.
If BLOCK or no setup: say so plainly and why. Never invent a setup the script didn't produce.
To change the rules, edit `entry.py` together with the user and say exactly what changed.
