# Deploying tradebot to a VPS (paper mode)

Goal: the bot runs 24/7 in **paper mode**, you approve proposals on Telegram, and the paper clock
(Ticket 12) starts. **Live trading stays off.** Nothing here needs exchange API keys.

## 1. Pick a VPS

- Region: **India** (Mumbai or Bangalore). That gives low latency to Delta India and an Indian static IP, which Angel One / SEBI rules will need later.
- Size: 1–2 vCPU, **2 GB RAM**, 25 GB SSD is plenty (≈ $6–12/month).
- OS: **Ubuntu 24.04 LTS**, with a **static/reserved IPv4**.
- Add your SSH public key when creating it. Don't use password login.

## 2. Bootstrap (once, as root)

```bash
scp deploy/setup_vps.sh root@VPS_IP:/root/
ssh root@VPS_IP bash /root/setup_vps.sh
```

This installs Docker, adds a `trader` user and an SSH-only firewall, enables auto security updates, fail2ban, UTC time sync and swap, and sets up a daily backup cron.

Check you can log in as the new user (`ssh trader@VPS_IP`). Then lock SSH down:

```bash
ssh root@VPS_IP 'HARDEN_SSH=1 bash /root/setup_vps.sh'
```

## 3. Copy the project (from your Mac)

```bash
rsync -av --exclude .venv --exclude __pycache__ --exclude logs --exclude reports \
  --exclude backups --exclude .env --exclude delta-exchange-mcp \
  ~/Desktop/Tradingbot/ trader@VPS_IP:~/tradebot/
```

`data/market.duckdb` (about 2.7 years of candles) is copied along. That saves a re-download, but you can also rebuild it on the VPS (step 6).

## 4. Secrets and config (on the VPS)

```bash
cd ~/tradebot
cp .env.example .env && chmod 600 .env && nano .env
cp config/local.example.yaml config/local.yaml && nano config/local.yaml
```

| Setting | Paper phase |
|---|---|
| `TELEGRAM_BOT_TOKEN` | **required**: get one from @BotFather |
| `telegram.allowed_user_ids` (local.yaml) | **required**: your numeric id (from @userinfobot) |
| `HEALTHCHECKS_PING_URL` | recommended: a free healthchecks.io check, which alerts you if the whole VPS dies |
| `ANTHROPIC_API_KEY` | optional: LLM news labels (otherwise keyword rules) |
| `DELTA_API_KEY/SECRET` | **leave empty**: paper mode only needs public market data |
| `TRADEBOT_ALLOW_LIVE` | **false** |

## 5. Build and start

```bash
sudo chown -R 10001:10001 data logs reports backups   # container user
docker compose build
docker compose run --rm bot tradebot check            # config sanity: must say PAPER MODE
docker compose up -d                                   # bot + watchdog
```

## 6. Verify (first 10 minutes)

```bash
docker compose ps                                      # bot "healthy" after ~3 min
docker compose logs -f bot                             # "tradebot started (paper)"
docker compose run --rm bot tradebot data status       # data current? if empty:
docker compose run --rm bot tradebot data backfill --market crypto --res 1h,5m --days 1000
docker compose run --rm bot tradebot data verify       # funding schedule checks: OK
```

In Telegram, send your bot `/status`, `/pnl` and `/paper`. Then try the whole approval flow once with a demo proposal:

```bash
docker compose run --rm bot tradebot proposals demo BTCUSD buy
```

Tap APPROVE in Telegram. You should get a fill and see the exchange stop under `/positions`. Then close it:

```bash
docker compose run --rm bot tradebot exec close <ID>
```

Before the real paper phase, wipe the demo state so it doesn't count:

```bash
docker compose down && sudo rm -f data/forward.db data/proposals.db data/risk_state.db \
  data/execution.db data/paper_exchange.json data/runtime_state.json && docker compose up -d
```

The paper clock starts at that `up -d`.

## 7. Day-to-day

| Task | Command |
|---|---|
| Live logs | `docker compose logs -f bot` |
| Dashboard | `docker compose --profile dashboard up -d`, then on your Mac `ssh -L 8501:localhost:8501 trader@VPS_IP` and open http://localhost:8501 |
| Paper report | `/paper` in Telegram, or `docker compose run --rm bot tradebot paper report` |
| Pause / resume | `/pause`, `/resume` |
| Emergency | `/killswitch` (confirm tap) |
| Manual backup | `docker compose run --rm bot tradebot backup --dest /app/backups` |
| Off-site backup | copy `~/tradebot/backups/` elsewhere weekly (e.g. `rsync` to your Mac). **The audit logs are your tax records.** |

**Updating code:** rsync again from your Mac, then `docker compose build && docker compose up -d`. A restart is safe: pending proposals, positions, halts and the paper clock are all on disk, and exchange-side stops don't depend on the bot.

**Restoring:** stop the bot, `tar xzf backups/tradebot-<ts>.tar.gz -C ~/tradebot`, start it again.

## 8. NOT in this step: before any live trading

This is the gated part of Ticket 13, done only after the Ticket 12 checklist is all green:

- Use a **dedicated Delta sub-account** for the bot. The bot treats any position it didn't open as an orphan and halts.
- Create an API key with **trading permission only (no withdrawals)**, **IP-whitelisted to this VPS's static IP**.
- Run on the **Delta testnet** first, especially to verify the stop-move endpoint (`PUT /orders/bracket`).
- Set `TELEGRAM_APPROVAL_PIN`, `mode: live`, the confirm phrase and `TRADEBOT_ALLOW_LIVE=true`, with **small capital** (`live.max_capital_inr`).
- Tax export for crypto (30% + 1% TDS rules).
