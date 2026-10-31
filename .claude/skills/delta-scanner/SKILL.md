---
name: delta-scanner
description: Scan Delta Exchange crypto/gold perpetuals for trend, levels and setups (BTC, ETH, SOL, XRP, XAUT...). Use when the user says scan, "what's the trend", "pull the trend", or asks which coins look interesting.
---

# delta-scanner

1. Read the account first with the Delta MCP (`get_wallet_balances`, `get_positions`). Flag any open
   position with no stop-loss before anything else.
2. Run from `delta-assistant/`:
   - `python3 scan.py --balance <wallet USD> [SYMBOLS]` — trend (EMA 20/50/200) on 4h/1h/15m, RSI, ATR,
     volume, nearest support/resistance, cost of 1 contract.
   - `python3 entry.py [SYMBOLS]` — whether the written strategy rules (see delta-entry) see a setup.
3. Report as a short table: symbol, price, 4h / 1h / 15m trend, key level, setup yes/no.
4. Times in UK time with UTC alongside. Watchlist and rules live in `delta-assistant/rules.json`.

Never call a setup "the best trade" or tell the user to buy/sell. Say what the rules output; "no setup"
is a normal answer. Not financial advice.
