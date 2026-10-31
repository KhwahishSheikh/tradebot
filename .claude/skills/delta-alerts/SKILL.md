---
name: delta-alerts
description: Price alerts on Delta symbols (above / below a price, or a % move). Use when the user says "alert me", "tell me when BTC hits", or wants to watch a level.
---

# delta-alerts

Run in `delta-assistant/`:
- `python3 alerts.py add SYMBOL above|below PRICE --note "..."`, or `add SYMBOL move PCT`
- `python3 alerts.py list` / `remove ID`
- `python3 alerts.py check --notify` — fires each alert once (macOS notification with --notify).

Alerts are stored in `delta-assistant/alerts.json` (gitignored). For hands-off checking, offer a
`/loop 5m` or scheduled task that runs ONLY `alerts.py check --notify`. An alert never places or
changes an order; acting on it goes through delta-broker with the user's yes.
