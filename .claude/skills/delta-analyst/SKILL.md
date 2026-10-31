---
name: delta-analyst
description: Health check of a Delta futures market - funding (vs last 30 days), open interest vs price (new longs/shorts, covering, liquidations), basis vs spot, order-book depth and slippage. Use when the user asks "is BTC crowded", "what's funding", "who is in control", or wants the fundamentals of a coin.
---

# delta-analyst

Run `python3 analyst.py [SYMBOLS]` in `delta-assistant/` (public data, no keys).

Explain in plain words, per symbol:
- **Funding**: who pays whom, and whether it's unusual vs the last 30 days ("crowded long/short" flags).
- **Open interest vs price** (4h / 24h / 7d): new longs, new shorts, shorts covering, or longs closing.
- **Basis**: futures above spot = buyers paying up; below = sellers pressing.
- **Order book**: depth each side, imbalance, slippage of a $10k order. Mention the note when the API
  only shows part of the book.

This describes positioning only. Never turn it into "buy" or "sell" — at most say it agrees or
disagrees with the trend from delta-scanner. Not financial advice.
