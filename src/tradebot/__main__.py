"""CLI entry point.

  tradebot check   validate config + secrets, print the resolved setup (no network calls)
  tradebot run     start the bot (Ticket 1: boots, logs, audits, exits)
"""

from __future__ import annotations

import argparse
import logging
import sys

from tradebot.audit import AuditEvent
from tradebot.config import LiveModeError, load_config
from tradebot.core.models import Market
from tradebot.logging_setup import configure_logging

log = logging.getLogger("tradebot")


def _banner(cfg) -> str:
    if cfg.is_live:
        return "!!! LIVE MODE: REAL MONEY !!!"
    return "PAPER MODE: simulated orders only"


def cmd_check(args) -> int:
    cfg, secrets = load_config(args.config_dir, args.env_file)
    print(_banner(cfg))
    print(f"capital: ₹{cfg.account.capital_inr:,.0f}   risk/trade: {cfg.risk.max_risk_per_trade_pct}%"
          f"   daily loss: {cfg.risk.max_daily_loss_pct}%   max DD: {cfg.risk.max_drawdown_pct}%")
    print(f"crypto (Delta): {'on' if cfg.crypto.enabled else 'off'} {cfg.crypto.symbols} "
          f"lev≤{cfg.crypto.max_leverage}x")
    print(f"equity (Angel One): {'on' if cfg.equity.enabled else 'off'} {cfg.equity.watchlist}")
    print(f"approval: intraday {cfg.approval.expiry_minutes.intraday}m, "
          f"swing {cfg.approval.expiry_minutes.swing}m, drift ≤{cfg.approval.max_price_drift_pct}%, "
          f"auto-approve {'ON' if cfg.approval.auto_approve.enabled else 'off'}")
    print("secrets:", ", ".join(f"{k}={'set' if v else 'MISSING'}" for k, v in secrets.status().items()))
    if not cfg.telegram.allowed_user_ids:
        print("warning: telegram.allowed_user_ids is empty: nobody can approve trades yet")
    return 0


def cmd_run(args) -> int:
    """Start the bot (runs until Ctrl-C / SIGTERM). --once: a single loop iteration (smoke test)."""
    from tradebot.runtime import Runtime
    cfg, secrets = load_config(args.config_dir, args.env_file)
    configure_logging(cfg, secrets)
    log.warning(_banner(cfg))
    rt = Runtime(cfg, secrets)
    if args.once:
        rt.audit.record(AuditEvent.SYSTEM_START, mode=cfg.mode.value, once=True)
        rt.step()
        print(rt.status_text())
        return 0
    rt.run()
    return 0


def _telegram_sender(cfg, secrets):
    if secrets.telegram_bot_token and cfg.telegram.allowed_user_ids:
        from tradebot.monitoring.telegram import TelegramAPI
        api = TelegramAPI(secrets.telegram_bot_token.get_secret_value())
        return lambda text: [api.send(c, text) for c in cfg.telegram.allowed_user_ids]
    return print


def cmd_watchdog(args) -> int:
    import os

    from tradebot.monitoring.health import watchdog
    cfg, secrets = load_config(args.config_dir, args.env_file)
    ok, msg = watchdog(cfg, _telegram_sender(cfg, secrets))
    print(msg)
    url = os.environ.get("HEALTHCHECKS_PING_URL")
    if ok and url:   # dead-man's switch: an outside service alerts you if these pings stop
        import httpx
        try:
            httpx.get(url, timeout=10)
        except httpx.HTTPError as e:
            print(f"ping failed: {e}")
    return 0 if ok else 1


def cmd_health(args) -> int:
    """Quiet liveness probe for Docker HEALTHCHECK: exit 0 if the heartbeat is fresh."""
    from datetime import datetime

    from tradebot.monitoring.health import read_heartbeat
    from tradebot.timeutil import now_utc
    cfg, _ = load_config(args.config_dir, args.env_file)
    hb = read_heartbeat(cfg.monitoring.heartbeat_file)
    if not hb:
        print("no heartbeat")
        return 1
    age = (now_utc() - datetime.fromisoformat(hb["ts"])).total_seconds()
    print(f"heartbeat {age:.0f}s ago")
    return 0 if age <= cfg.monitoring.watchdog_stale_seconds else 1


def cmd_backup(args) -> int:
    """Consistent snapshot of state + audit/tax logs -> backups/tradebot-<ts>.tar.gz (keeps N)."""
    import sqlite3
    import tarfile
    import tempfile
    from datetime import datetime
    from pathlib import Path
    cfg, _ = load_config(args.config_dir, args.env_file)
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"tradebot-{datetime.now():%Y%m%d-%H%M%S}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(out, "w:gz") as tar:
        for db in sorted(cfg.data.db_path.parent.glob("*.db")):
            snap = Path(tmp) / db.name
            src, dst = sqlite3.connect(db), sqlite3.connect(snap)
            src.backup(dst)                          # safe while the bot is writing
            src.close()
            dst.close()
            tar.add(snap, arcname=f"data/{db.name}")
        for extra in (cfg.execution.paper_state_file, cfg.monitoring.state_file):
            if extra.exists():
                tar.add(extra, arcname=f"data/{extra.name}")
        audit = Path(cfg.logging.dir) / "audit"
        if audit.exists():
            tar.add(audit, arcname="logs/audit")     # audit trail = trade + tax records
        local = Path(args.config_dir) / "local.yaml"
        if local.exists():
            tar.add(local, arcname="config/local.yaml")
    old = sorted(dest.glob("tradebot-*.tar.gz"))[:-args.keep]
    for f in old:
        f.unlink()
    print(f"backup {out} ({out.stat().st_size / 1024:.0f} KB); removed {len(old)} old")
    return 0


def cmd_summary(args) -> int:
    from tradebot.execution.orders import ExecutionStore
    from tradebot.monitoring.health import daily_summary
    from tradebot.proposals.store import ProposalStore
    from tradebot.risk.state import RiskStateStore
    cfg, _ = load_config(args.config_dir, args.env_file)
    d = cfg.data.db_path.parent
    print(daily_summary(cfg, ProposalStore(d / "proposals.db"), ExecutionStore(cfg.execution.db_path),
                        RiskStateStore(d / "risk_state.db", cfg.account.capital_inr)))
    return 0


def cmd_dashboard(args) -> int:
    import subprocess
    import sys as _sys
    from pathlib import Path
    app = Path(__file__).parent / "monitoring" / "dashboard.py"
    return subprocess.call([_sys.executable, "-m", "streamlit", "run", str(app),
                            "--server.address", "127.0.0.1", "--server.headless", "true"])


def cmd_clock(args) -> int:
    from datetime import datetime

    from tradebot.core.models import Horizon
    from tradebot.timeutil import IST, now_utc
    from tradebot.timing.clock import TradingClock

    cfg, _ = load_config(args.config_dir, args.env_file)
    clock = TradingClock(cfg)
    at = datetime.fromisoformat(args.at).replace(tzinfo=IST) if args.at else now_utc()
    print(f"at {at.astimezone(IST):%a %d %b %Y %H:%M} IST   "
          f"you available: {'yes' if clock.user_available(at) else 'NO (quiet hours)'}")
    markets = [m for m, on in ((Market.NSE, cfg.equity.enabled),
                               (Market.CRYPTO_DELTA, cfg.crypto.enabled)) if on]
    for market in markets:
        st = clock.status(market, at)
        print(f"\n{market.value}: {st.phase} ({st.detail})"
              + (f"  [{st.next_event}]" if st.next_event else ""))
        for note in st.notes:
            print(f"  note: {note}")
        for h in Horizon:
            d = clock.can_propose(market, h, at)
            print(f"  propose {h.value:<9} {'YES' if d else 'no '}  {'; '.join(d.reasons)}")
        print(f"  protective exits: {'YES' if clock.can_exit(market, at) else 'no'}")
    print()
    for u in ("NIFTY", "BANKNIFTY") if cfg.equity.enabled else ():
        nxt = clock.nse.next_expiry(cfg.timing.nse.expiries, u, at.astimezone(IST).date())
        print(f"next {u} expiry: {nxt[0]:%a %d %b %Y} ({nxt[1]})" if nxt else f"{u}: unknown")
    return 0


def cmd_signals(args) -> int:
    from tradebot.data.store import MarketStore
    from tradebot.strategies.engine import SignalEngine
    from tradebot.timeutil import IST

    cfg, _ = load_config(args.config_dir, args.env_file)
    store = MarketStore(cfg.data.db_path)
    news = None
    if cfg.news.enabled and cfg.news.use_in_signals:
        from tradebot.news.score import NewsScorer
        from tradebot.news.store import NewsStore
        news = NewsScorer(NewsStore(cfg.news.db_path), cfg.news.lookback_hours)
    evs = SignalEngine(cfg, store, news).latest()
    if not evs:
        print("no setups on the latest closed bar")
    for ev in evs:
        s = ev.setup
        tag = "SIGNAL " if ev.passed else "blocked"
        print(f"[{tag}] {ev.strategy} {ev.symbol} {s.side.value.upper()} @ {s.entry:,.1f}  "
              f"SL {s.stop:,.1f}  TP {s.target:,.1f}  R:R net {ev.net_rr:.2f}  "
              f"conf {ev.confidence:.2f}  ({ev.ts.astimezone(IST):%d %b %H:%M} IST bar)")
        print("    " + (ev.signal.reasoning if ev.signal else "; ".join(ev.blocked_by)))
    store.close()
    return 0


def cmd_study(args) -> int:
    import pandas as pd

    from tradebot.data.store import MarketStore
    from tradebot.strategies.engine import SignalEngine
    from tradebot.strategies.study import study, summarize

    cfg, _ = load_config(args.config_dir, args.env_file)
    store = MarketStore(cfg.data.db_path)
    engine = SignalEngine(cfg, store)
    trades = []
    for entry in cfg.strategies.crypto:
        if args.strategies and entry.name not in args.strategies.split(","):
            continue
        strat = engine.build(entry)
        for sym in entry.symbols:
            trades += study(engine, strat, sym)
    store.close()
    pd.set_option("display.width", 200)
    passed = [t for t in trades if t.passed]
    print("== ALL setups (before gates) ==")
    print(summarize(trades, ["strategy"]).to_string(index=False))
    print("\n== AFTER gates (what you would have been sent) ==")
    print(summarize(passed, ["strategy", "symbol"]).to_string(index=False))
    print("\n== AFTER gates, by regime ==")
    print(summarize(passed, ["strategy", "regime"]).to_string(index=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tradebot")
    p.add_argument("--config-dir", default="config")
    p.add_argument("--env-file", default=".env")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    run = sub.add_parser("run", help="start the bot")
    run.add_argument("--once", action="store_true", help="one loop iteration, then exit")
    run.set_defaults(fn=cmd_run)
    sub.add_parser("watchdog", help="alert if the bot's heartbeat is stale (cron)").set_defaults(fn=cmd_watchdog)
    sub.add_parser("summary", help="today's summary").set_defaults(fn=cmd_summary)
    sub.add_parser("health", help="exit 0 if heartbeat is fresh (Docker healthcheck)").set_defaults(fn=cmd_health)
    bk = sub.add_parser("backup", help="snapshot state DBs + audit logs (no secrets)")
    bk.add_argument("--dest", default="backups")
    bk.add_argument("--keep", type=int, default=30)
    bk.set_defaults(fn=cmd_backup)
    sub.add_parser("dashboard", help="Streamlit dashboard on 127.0.0.1:8501").set_defaults(fn=cmd_dashboard)
    clock = sub.add_parser("clock", help="show market status and trading gates")
    clock.add_argument("--at", help="IST time to evaluate, e.g. 2026-10-19T09:20")
    clock.set_defaults(fn=cmd_clock)
    sub.add_parser("signals", help="evaluate strategies on the latest closed bar"
                   ).set_defaults(fn=cmd_signals)
    st = sub.add_parser("study", help="compare strategies on stored history (pre-backtest)")
    st.add_argument("--strategies", help="comma separated names (default: all configured)")
    st.set_defaults(fn=cmd_study)
    from tradebot.data.cli import register as register_data
    register_data(sub)
    from tradebot.risk.cli import register as register_risk
    register_risk(sub)
    from tradebot.news.cli import register as register_news
    register_news(sub)
    from tradebot.backtest.cli import register as register_backtest
    register_backtest(sub)
    from tradebot.proposals.cli import register as register_proposals
    register_proposals(sub)
    from tradebot.execution.cli import register as register_exec
    register_exec(sub)
    from tradebot.execution.cli import register_exits
    register_exits(sub)
    from tradebot.paper.cli import register as register_paper
    register_paper(sub)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except LiveModeError as e:
        print(e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
