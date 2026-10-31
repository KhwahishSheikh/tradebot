---
name: delta-risk
description: PASS/BLOCK check of a proposed Delta trade against the user's rules (risk %, leverage, R:R, liquidation buffer, open positions, daily loss, funding, trading window). Use before any trade idea or when the user asks "can I take this trade".
---

# delta-risk

Run in `delta-assistant/`:
`python3 risk.py SYMBOL buy|sell --stop S --target T [--entry E] [--contracts N] [--leverage L]`
(account read-only from .env keys; offline: `--balance B --open N --day-pnl P`). Exit code 0 = PASS, 2 = BLOCK.

Rules come from `delta-assistant/rules.json` (1% risk incl. fees, max 3x, max 2 positions, 3% daily loss,
R:R >= 1.5, liquidation >= 2x stop distance). WARN lines (funding, trading window) don't block but must be shown.

Show every BLOCK line in plain words. A BLOCK is final: never suggest raising leverage, dropping the stop
or resizing just to get a PASS. Only the user may change rules.json, and you say what it changes.
