# delta-assistant

A small toolkit for trading **Delta Exchange India** perpetuals together with **Claude Code**.
Each tool is a plain Python script and a `/delta-*` skill in [`../.claude/skills`](../.claude/skills).
Claude reads the market and your account; **orders only go out through `trade.py`, as a dry run first, and only after your "yes"**.

> ⚠️ Not financial advice. Leveraged futures can wipe out an account in minutes. Use testnet first.

| Skill | Script | What it does |
|---|---|---|
| `/delta-scanner` | `scan.py` | Trend (EMA 20/50/200) on 4h/1h/15m, RSI, ATR, volume, support/resistance |
| `/delta-entry` | `entry.py` | Written strategy rules: **trend pullback** and **London opening-range breakout** |
| `/delta-exit` | `exit.py` | Exit plan for open positions: missing stop, break-even, trailing, trend flip, funding |
| `/delta-risk` | `risk.py` | **PASS / BLOCK** against `rules.json` (risk %, leverage, R:R, liquidation buffer, daily loss) |
| `/delta-budget` | `size.py` | Contracts that fit your risk, margin, liquidation estimate |
| `/delta-broker` | `trade.py` | Bracket orders (stop-loss + take-profit on the exchange), protect, close, cancel. Dry-run unless `--confirm` |
| `/delta-timemachine` | `timemachine.py` | Replay over real history + 10,000 Monte Carlo paths: target-first vs stop-first |
| `/delta-alerts` | `alerts.py` | Price alerts (above / below / % move), optional macOS notification |
| `/delta-news` | `news.py` | CoinDesk / Cointelegraph / Decrypt headlines for your watchlist, HIGH-impact flags |

## Setup

```bash
cd delta-assistant
pip install -r requirements.txt
cp .env.example .env              # your keys stay local; LIVE=false = testnet (fake money)
cp journal.example.csv journal.csv
```

Create API keys with **trading permission only, no withdrawals**. Edit your limits in [`rules.json`](rules.json).

## Examples

```bash
python3 entry.py                                   # any setups on the watchlist?
python3 risk.py BTCUSD buy --stop 76800 --target 78700
python3 size.py ETHUSD sell --stop 2290 --balance 200
python3 trade.py buy XRPUSD --contracts 1 --stop 1.20 --target 1.40     # dry run
python3 trade.py protect XAUTUSD --stop 3999.7                          # add a stop to an open trade
python3 timemachine.py BTCUSD buy --stop-pct 0.6 --target-pct 1.2
python3 alerts.py add BTCUSD below 76000 --note "support break"
python3 news.py --hours 24
```

Market data is public (no keys). Account reads and orders use the keys in `.env`.
