# tradebot

![tests](https://github.com/KhwahishSheikh/tradebot/actions/workflows/tests.yml/badge.svg)

Human-in-the-loop trading system for **Delta Exchange India** (crypto perpetuals) and
**NSE via Angel One SmartAPI**. The bot finds setups, sizes them against hard risk limits and
sends a proposal; **nothing is traded until you approve it**. Paper mode by default.

> ⚠️ **Disclaimer.** Educational project, not financial advice. Leveraged derivatives can lose
> more than you expect, very fast. Use paper/testnet first and only risk money you can afford to lose.

## Features

- **Data**: Delta candles, funding and open interest, NSE candles → DuckDB
- **Strategies**: signal + confidence, regime filter, backtest and walk-forward (out-of-sample)
- **Risk engine**: per-trade risk %, daily loss limit, drawdown halt, leverage cap,
  liquidation-buffer check, event blackouts, kill switch
- **Approval flow**: proposals with expiry and price-drift checks, approve / reject / modify
  via Telegram (PIN required in live mode)
- **Execution**: paper or live broker, exchange-side stop-losses, reconcile, protective exits
- **Monitoring**: Telegram alerts, watchdog, health check, Streamlit dashboard, append-only audit log
- **News**: RSS feeds classified by Claude (keyword fallback) as a confidence modifier

Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · VPS deploy (Docker):
[deploy/DEPLOY.md](deploy/DEPLOY.md)

## Repository layout

```
src/tradebot/        the bot (data, strategies, risk, proposals, execution, monitoring, news)
tests/               pytest suite (no network or API keys needed)
config/              default.yaml + local.example.yaml + market calendars
delta-assistant/     small standalone scan/trade scripts for use with Claude Code
delta-exchange-mcp/  MCP server exposing the Delta REST API to Claude (read-only by default)
deploy/, docs/       VPS setup and architecture notes
```

## Quick start

Needs Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/KhwahishSheikh/tradebot.git && cd tradebot
uv sync                      # create env + install deps
cp .env.example .env         # add YOUR keys locally (never commit .env)
uv run tradebot check        # validate config
uv run pytest                # run the tests
```

## Commands

```bash
uv run tradebot check        # validate config, show resolved setup
uv run tradebot clock        # market status + trading gates now
uv run tradebot data backfill --market crypto --res 1h   # Delta candles/funding/OI -> DuckDB
uv run tradebot data backfill --market nse --res 5m,1d   # needs ANGEL_* in .env
uv run tradebot data status  # coverage + gaps
uv run tradebot data verify  # funding times + F&O expiries vs exchanges
uv run tradebot signals      # strategies on the latest closed bar
uv run tradebot study        # compare strategies on stored history (costs incl.)
uv run tradebot risk status  # equity, drawdown, halts, next event blackout
uv run tradebot risk preview BTCUSD buy   # size + check a hypothetical trade at live price
uv run tradebot risk killswitch --reason "..."   # block entries + cancel orders + close bot positions
uv run tradebot risk resume  # clear halts
uv run tradebot news update  # fetch feeds + classify new headlines
uv run tradebot news digest  # watchlist digest
uv run tradebot backtest run [--delay 3] [--ignore-quiet-hours]   # full-history backtest
uv run tradebot backtest wfo  # walk-forward, out-of-sample only (~2 min)
uv run tradebot proposals demo BTCUSD buy   # try the approval flow (paper)
uv run tradebot proposals list | show ID | approve ID | reject ID | modify ID --contracts 3
uv run tradebot exec status  # open bot positions, uPnL, exchange-stop check
uv run tradebot exec reconcile | close ID
uv run tradebot exits tick | review | pending | answer ID hold|close
uv run tradebot run          # THE BOT (paper). --once for a single loop
uv run tradebot watchdog     # cron every 2 min: alerts on Telegram if the bot stops
uv sync --extra dashboard && uv run tradebot dashboard   # http://127.0.0.1:8501
uv run tradebot paper report # paper-phase report + go-live checklist
uv run tradebot health | backup   # liveness probe; state + audit-log snapshot
```

## Configuration

- **Secrets** go in `.env` (copy from [.env.example](.env.example)). It is gitignored.
- **Settings** go in `config/local.yaml` (copy from
  [config/local.example.yaml](config/local.example.yaml)), which overrides `config/default.yaml`. Also gitignored.

```yaml
account:
  capital_inr: 50000
telegram:
  allowed_user_ids: [123456789]
```

Going live needs **all three**: `mode: live`, the confirm phrase in YAML, and
`TRADEBOT_ALLOW_LIVE=true` in `.env`.

## Telegram setup (2 minutes)

1. In Telegram, talk to **@BotFather** → `/newbot` → copy the token into `.env` as `TELEGRAM_BOT_TOKEN`.
2. Get your numeric user id (e.g. from **@userinfobot**) and put it in `config/local.yaml`
   under `telegram.allowed_user_ids`.
3. Start the bot (`uv run tradebot run`) and send your bot `/help`. Anyone else is ignored (and audited).
4. Before any live trading, set `TELEGRAM_APPROVAL_PIN` in `.env`: approvals then need the PIN.

## Security

- Never commit `.env`, `config/local.yaml`, `data/`, logs or trade journals (all gitignored).
- Create exchange API keys with **trading permission only, no withdrawals**, and IP-whitelist them.
- If a key is ever pushed by mistake, **revoke it on the exchange immediately**. Deleting the
  commit is not enough.

## License

[MIT](LICENSE)
