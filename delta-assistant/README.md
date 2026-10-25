# delta-assistant

Small standalone helpers for trading Delta Exchange India **together with Claude Code**.
Claude reads the market and account through the Delta MCP server (`../delta-exchange-mcp`),
and the rules it follows live in [CLAUDE.md](CLAUDE.md).

| File | What it does |
|---|---|
| `scan.py` | Public market data only: trend (EMA 20/50/200), RSI, ATR, volume, support/resistance on 4h/1h/15m, plus the widest stop that fits your risk % |
| `CLAUDE.md` | Golden rules, risk rules and the trade-proposal format |
| `journal.example.csv` | Trade journal header. Copy it to `journal.csv` (gitignored) |

```bash
pip install -r requirements.txt
cp .env.example .env                  # keys stay local; LIVE=false = testnet
cp journal.example.csv journal.csv
python scan.py --balance 100          # default symbols
python scan.py --balance 100 BTCUSD ETHUSD
```

Not financial advice. Use testnet first, and always trade with a stop-loss.
