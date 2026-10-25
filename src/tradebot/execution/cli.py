"""`tradebot exec ...`: positions, reconcile, close, flatten (paper unless live is validated)."""

from __future__ import annotations

import argparse

from tradebot.config import load_config
from tradebot.execution.factory import build_execution
from tradebot.risk.cli import build_manager


def _build(cfg, secrets):
    mgr, _ = build_manager(cfg, secrets)
    return build_execution(cfg, secrets, mgr.engine.specs, mgr.store, mgr.audit)


def format_positions(ex, cfg) -> str:
    rows = ex.store.positions()
    if not rows:
        return "No open bot positions."
    out = []
    for p in rows:
        last = ex.x.last_price(p.symbol)
        sgn = 1 if p.side == "buy" else -1
        upnl = sgn * (last - p.entry_price) * p.contracts * p.contract_value * cfg.crypto.usd_inr
        out.append(f"#{p.proposal_id} {p.symbol} {'LONG' if p.side == 'buy' else 'SHORT'} {p.contracts}c "
                   f"@ {p.entry_price:,.2f} | last {last:,.2f} | {p.r_multiple(last):+.2f}R | "
                   f"uPnL ₹{upnl:+,.0f}\n  SL {p.stop:,.2f}  TP {p.target:,.2f}  exchange stop "
                   f"{'OK' if p.bracket_ok else 'MISSING'}  exit by {p.exit_by[:16]} UTC")
    return "\n".join(out)


def cmd_status(args, cfg, secrets) -> int:
    ex = _build(cfg, secrets)
    print(f"exchange: {type(ex.x).__name__} (paper={ex.x.is_paper})")
    print(format_positions(ex, cfg))
    st = ex.risk_store.state()
    print(f"equity ₹{st.equity_inr:,.0f}" + (" (paper ledger)" if ex.x.is_paper else ""))
    return 0


def cmd_reconcile(args, cfg, secrets) -> int:
    ev = _build(cfg, secrets).reconcile()
    print("in sync" if not ev else "\n".join(ev))
    return 0


def cmd_close(args, cfg, secrets) -> int:
    ok = _build(cfg, secrets).close(args.id, "manual close")
    print("closed" if ok else "no such open position")
    return 0 if ok else 1


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("exec", help="execution: positions, reconcile, close")
    es = p.add_subparsers(dest="ex_cmd", required=True)
    es.add_parser("status").set_defaults(ex_fn=cmd_status)
    es.add_parser("reconcile").set_defaults(ex_fn=cmd_reconcile)
    c = es.add_parser("close")
    c.add_argument("id", help="proposal id of the position")
    c.set_defaults(ex_fn=cmd_close)
    p.set_defaults(fn=lambda args: args.ex_fn(args, *load_config(args.config_dir, args.env_file)))


def _pm(cfg, secrets):
    from tradebot.data.store import MarketStore
    from tradebot.execution.exits import PositionManager
    from tradebot.news.score import NewsScorer
    from tradebot.news.store import NewsStore
    from tradebot.strategies.engine import SignalEngine
    from tradebot.timing.clock import TradingClock
    ms = MarketStore(cfg.data.db_path)
    se = SignalEngine(cfg, ms)
    news = NewsScorer(NewsStore(cfg.news.db_path), cfg.news.lookback_hours) if cfg.news.enabled else None
    return PositionManager(cfg, _build(cfg, secrets), print, features=lambda s: se.features(s, "1h"),
                           news=news, trading_clock=TradingClock(cfg))


def cmd_tick(args, cfg, secrets) -> int:
    ev = _pm(cfg, secrets).tick()
    print("nothing to do" if not ev else "\n".join(ev))
    return 0


def cmd_review(args, cfg, secrets) -> int:
    print(_pm(cfg, secrets).daily_review())
    return 0


def cmd_pending(args, cfg, secrets) -> int:
    reqs = _build(cfg, secrets).store.requests()
    print("no open questions" if not reqs else "\n".join(
        f"[{r.id}] {r.kind} #{r.proposal_id}: {r.detail} (expires {r.expires_at[11:16]} UTC, "
        f"default {r.default_action})" for r in reqs))
    return 0


def register_exits(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("exits", help="hold/exit logic: tick, daily review, answer questions")
    xs = p.add_subparsers(dest="x_cmd", required=True)
    xs.add_parser("tick", help="reconcile + trailing + max-hold + timeouts").set_defaults(x_fn=cmd_tick)
    xs.add_parser("review", help="daily position review (alerts only)").set_defaults(x_fn=cmd_review)
    xs.add_parser("pending", help="open hold/close questions").set_defaults(x_fn=cmd_pending)
    a = xs.add_parser("answer")
    a.add_argument("id")
    a.add_argument("action", choices=["hold", "close"])
    a.set_defaults(x_fn=lambda args, c, s: print(_pm(c, s).answer(args.id, args.action, "cli")))
    p.set_defaults(fn=lambda args: args.x_fn(args, *load_config(args.config_dir, args.env_file)))
