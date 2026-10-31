---
name: delta-budget
description: Position size for a Delta trade - how many contracts fit the risk rule, margin, liquidation estimate, and the minimum balance needed. Use when the user asks "how many contracts", "how much can I trade", or "what size".
---

# delta-budget

Run in `delta-assistant/`:
`python3 size.py SYMBOL buy|sell --stop S [--entry E] [--target T] [--balance B] [--leverage L]`

Contracts = floor(risk budget / (stop distance x contract value + taker fees both ways)).
If it prints 0 contracts, say so and quote the "1 contract needs a balance of about $X" line —
never suggest a bigger size or tighter stop to make it fit. Contract values: BTC 0.001, ETH 0.01,
SOL 1, XRP 1, DOGE 100, XAUT 0.001 (always read live from the product).
