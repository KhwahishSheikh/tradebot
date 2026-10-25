"""`tradebot proposals ...`: drive the approval flow from the shell (Telegram arrives in Ticket 11).
Shell commands act as user "cli" (whoever has shell access on the bot host)."""

from __future__ import annotations

import argparse
from datetime import timedelta
from decimal import Decimal

from tradebot.audit import AuditLog
from tradebot.config import load_config
from tradebot.core.models import Horizon, Instrument, Market, Side, Signal
from tradebot.proposals.engine import ConsoleNotifier, ProposalEngine
from tradebot.proposals.model import render
from tradebot.proposals.store import ProposalStore
from tradebot.risk.cli import build_manager
from tradebot.risk.engine import MarketContext
from tradebot.timeutil import IST, now_utc


def build(cfg, secrets) -> ProposalEngine:
    from tradebot.execution.factory import build_execution
    risk, delta = build_manager(cfg, secrets)
    audit = AuditLog(cfg.logging.dir, cfg.mode)
    store = ProposalStore(cfg.data.db_path.parent / "proposals.db")
    execu = build_execution(cfg, secrets, risk.engine.specs, risk.store, audit)
    risk.open_positions = execu.open_risk          # risk checks see what is actually open
    return ProposalEngine(cfg, risk, store, lambda s: float(delta.last_price(s)),
                          execu, audit, ConsoleNotifier())


def _print(out) -> int:
    print(("OK: " if out.ok else "NO: ") + out.message)
    for r in out.reasons:
        print("   -", r)
    if out.fresh:
        print()
        print(render(out.fresh, now_utc()))
    return 0 if out.ok else 1


def cmd_demo(args, cfg, secrets) -> int:
    """Hypothetical proposal at the live price (stop = N x ATR(1h), target = 2R) to try the flow."""
    from tradebot.data.store import MarketStore
    from tradebot.strategies.engine import SignalEngine
    eng = build(cfg, secrets)
    ms = MarketStore(cfg.data.db_path)
    f = SignalEngine(cfg, ms).features(args.symbol, "1h")
    ms.close()
    live = eng.price(args.symbol)
    atr, side = float(f["atr"].iat[-1]), Side(args.side)
    sgn = 1 if side is Side.BUY else -1
    stop, target = live - sgn * args.atr * atr, live + sgn * 2 * args.atr * atr
    bar_close = (f["ts"].iat[-1] + timedelta(hours=1)).to_pydatetime()
    sig = Signal(Instrument(Market.CRYPTO_DELTA, args.symbol), side, "manual_demo",
                 Horizon(args.horizon), Decimal(f"{live:.2f}"), Decimal(f"{stop:.2f}"),
                 Decimal(f"{target:.2f}"), 0.65, "DEMO proposal for testing the approval flow",
                 bar_close, "1h", str(f["regime"].iat[-1]), timedelta(hours=72))
    return _print(eng.create(sig, MarketContext(now_utc(), None, bar_close)))


def cmd_list(args, cfg, secrets) -> int:
    eng = build(cfg, secrets)
    eng.expire_due()
    rows = eng.store.pending() if not args.all else eng.store.recent(args.n)
    if not rows:
        print("no proposals" if args.all else "no pending proposals")
    for p in rows:
        print(f"#{p.id}  {p.status.value:<15} {p.symbol} {p.side:<4} {p.contracts:>4}c  "
              f"entry {p.entry:,.1f}  risk ₹{p.risk_inr:,.0f}  "
              f"{p.created_at.astimezone(IST):%d %b %H:%M} -> exp {p.expires_at.astimezone(IST):%H:%M} IST"
              + (f"  by {p.decided_by}" if p.decided_by else ""))
    return 0


def cmd_show(args, cfg, secrets) -> int:
    eng = build(cfg, secrets)
    got = eng.store.get(args.id)
    if not got:
        print("not found")
        return 1
    p, _ = got
    print(render(p, now_utc()))
    print("\nhistory:")
    for h in p.history:
        print("  ", h)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("proposals", help="trade proposals & approvals")
    ps = p.add_subparsers(dest="p_cmd", required=True)
    d = ps.add_parser("demo", help="create a hypothetical proposal at the live price")
    d.add_argument("symbol")
    d.add_argument("side", choices=["buy", "sell"])
    d.add_argument("--atr", type=float, default=2.0)
    d.add_argument("--horizon", choices=[h.value for h in Horizon], default="swing")
    d.set_defaults(p_fn=cmd_demo)
    ls = ps.add_parser("list")
    ls.add_argument("--all", action="store_true")
    ls.add_argument("-n", type=int, default=20)
    ls.set_defaults(p_fn=cmd_list)
    sh = ps.add_parser("show")
    sh.add_argument("id")
    sh.set_defaults(p_fn=cmd_show)
    a = ps.add_parser("approve")
    a.add_argument("id")
    a.set_defaults(p_fn=lambda args, c, s: _print(build(c, s).approve(args.id, "cli")))
    r = ps.add_parser("reject")
    r.add_argument("id")
    r.add_argument("--note", default="")
    r.set_defaults(p_fn=lambda args, c, s: _print(build(c, s).reject(args.id, "cli", args.note)))
    m = ps.add_parser("modify")
    m.add_argument("id")
    m.add_argument("--contracts", type=int)
    m.add_argument("--stop", type=float)
    m.add_argument("--target", type=float)
    m.set_defaults(p_fn=lambda args, c, s: _print(build(c, s).modify(
        args.id, "cli", args.contracts, args.stop, args.target)))
    p.set_defaults(fn=lambda args: args.p_fn(args, *load_config(args.config_dir, args.env_file)))
