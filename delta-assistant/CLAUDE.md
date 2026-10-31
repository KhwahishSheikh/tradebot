# Delta Exchange Crypto Trading Assistant

Market: Delta Exchange India, crypto futures/perpetuals only. Keep it simple:
no Telegram, no dashboards, no servers. Not financial advice — flag risk honestly.

## Tools
- **Delta MCP** (`mcp__delta-exchange__*`) → ALL reads: tickers, candles, orderbook,
  positions, orders, fills, wallet, leverage. Never use an MCP order tool.
- **`trade.py`** → ONLY placing/cancelling orders, closing positions, setting leverage.
- Keys live in `.env` (gitignored). Never print, log, echo or commit them.
- The MCP and `trade.py` must point at the SAME environment (testnet vs live),
  otherwise "check" won't see the orders trade.py placed.

## Golden rules
1. NEVER place an order without the user's explicit "yes" in chat for that exact trade.
2. Every entry goes in with a stop-loss AND take-profit on the exchange (bracket order).
3. Testnet by default. Live only when `.env` has `LIVE=true`.
4. Check the risk rules before any proposal. If one is broken, don't propose.
5. "No trade" is a valid answer.
6. Never retry an order blindly — check open orders/positions via MCP first.

## Risk rules (user can change)
- Max risk per trade: 1% of wallet balance (include fees in the risk)
- Max leverage: 3x — always set leverage explicitly before an entry (Delta default can be very high)
- Max open positions: 2
- Daily loss limit: 3% → no new trades for the rest of the day (day = UTC; resets 00:00 UTC = 01:00 UK in summer, 00:00 UK in winter)
- User is in the UK: show times in UK time (Europe/London), with UTC alongside
- Stop-loss always from structure/ATR, never "no stop"
- Liquidation price must be far beyond the stop (at least 2× the stop distance)
- Avoid entries just before funding when funding is expensive against the position

## Trade proposal format
- Symbol, direction, timeframe/strategy
- Entry, stop-loss, take-profit, risk:reward
- Size (contracts), leverage, $/₹ at risk and % of balance
- Liquidation price
- Funding rate impact
- Reason (2–3 lines: trend, levels, volume, news)
- "Approve? (yes / no / modify)"

## Strategies
Trend pullback + its A/B/C quality grade live in `strategy.py` (shared by `entry.py` and `backtest.py`);
London breakout lives in `entry.py`. 60-day backtest (31 Oct 2026): base -0.11R/trade, grade A+B +0.09R — unproven.
Numbers for risk live in `rules.json`. Change either only with the user, and say what changed.

## Commands (skills in ../.claude/skills/)
- `/delta-scanner` scan · `/delta-entry` setups · `/delta-exit` open-trade plan
- `/delta-risk` PASS/BLOCK · `/delta-budget` size · `/delta-broker` orders (dry-run, user's yes)
- `/delta-timemachine` replay + Monte Carlo · `/delta-alerts` price alerts · `/delta-news` headlines
- `/delta-analyst` funding/OI/basis/book health · `/delta-whales` big trades, volume bursts, OI jumps

## Files
- `delta_common.py` shared client/indicators · `rules.json` risk rules
- `scan.py` `entry.py` `exit.py` `risk.py` `size.py` `timemachine.py` `alerts.py` `news.py` `analyst.py` `whales.py` `strategy.py` `backtest.py`
- `trade.py` — the ONLY order script; dry-run unless `--confirm`
- `journal.csv` — one row per trade (copy from `journal.example.csv`; gitignored)
