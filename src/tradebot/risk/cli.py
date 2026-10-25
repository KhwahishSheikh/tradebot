"""`tradebot risk ...`: status, preview sizing, kill switch, resume."""

from __future__ import annotations

import argparse
from datetime import timedelta
from decimal import Decimal

from tradebot.audit import AuditLog
from tradebot.config import AppConfig, load_config
from tradebot.core.interfaces import RiskStage
from tradebot.core.models import Horizon, Instrument, Market, Side, Signal
from tradebot.risk.engine import CryptoRiskEngine, MarketContext
from tradebot.risk.events import EventCalendar
from tradebot.risk.manager import RiskManager
from tradebot.risk.sizing import load_specs
from tradebot.risk.state import HaltKind, RiskStateStore
from tradebot.timeutil import IST, now_utc
from tradebot.timing.clock import TradingClock


def build_manager(cfg: AppConfig, secrets) -> tuple[RiskManager, object]:
    from tradebot.data.cli import _delta
    delta = _delta(cfg, secrets)
    specs = load_specs(delta, cfg.crypto.symbols, cfg.data.db_path.parent / "delta_specs.json")
    engine = CryptoRiskEngine(cfg, specs, TradingClock(cfg), EventCalendar.from_yaml(cfg.risk.events_file))
    store = RiskStateStore(cfg.data.db_path.parent / "risk_state.db", cfg.account.capital_inr)
    return RiskManager(engine, store, AuditLog(cfg.logging.dir, cfg.mode)), delta


def cmd_status(args, cfg, secrets) -> int:
    mgr, _ = build_manager(cfg, secrets)
    s = mgr.store.state()
    r = cfg.risk
    print(f"equity ₹{s.equity_inr:,.0f}   peak ₹{s.peak_equity_inr:,.0f}   "
          f"drawdown {s.drawdown_pct:.2f}% / {r.max_drawdown_pct}%   "
          f"today {s.day_pnl_pct:+.2f}% / -{r.max_daily_loss_pct}%")
    n, _ = s.consecutive_losses()
    print(f"consecutive losses {n} / {r.max_consecutive_losses}   "
          f"risk per trade {r.max_risk_per_trade_pct}%   heat cap {r.max_portfolio_heat_pct}%   "
          f"gross cap {r.max_gross_leverage}x   order leverage {cfg.crypto.max_leverage}x")
    print("halts:", "none" if not s.halts else "")
    for h in s.halts:
        print(f"  [{h.kind.value}] {h.reason}" + (f" until {h.until.astimezone(IST):%d %b %H:%M} IST"
                                                    if h.until else " (manual resume)"))
    ev = mgr.engine.events.next_event(Market.CRYPTO_DELTA.value, now_utc())
    print(f"next event blackout: {ev.name} {ev.at.astimezone(IST):%a %d %b %H:%M} IST "
          f"(-{ev.before} / +{ev.after})" if ev else "next event: none listed: update events.yaml")
    return 0


def cmd_preview(args, cfg, secrets) -> int:
    """Hypothetical trade at the live price with a stop of N x ATR(1h): shows full sizing + checks."""
    from tradebot.data.store import MarketStore
    from tradebot.strategies.engine import SignalEngine
    mgr, delta = build_manager(cfg, secrets)
    store = MarketStore(cfg.data.db_path)
    f = SignalEngine(cfg, store).features(args.symbol, "1h")
    store.close()
    atr, last_ts = float(f["atr"].iat[-1]), f["ts"].iat[-1]
    live = float(delta.last_price(args.symbol))
    side = Side(args.side)
    sgn = 1 if side is Side.BUY else -1
    stop, target = live - sgn * args.atr * atr, live + sgn * 2 * args.atr * atr
    sig = Signal(Instrument(Market.CRYPTO_DELTA, args.symbol), side, "manual_preview", Horizon.SWING,
                 Decimal(f"{live:.2f}"), Decimal(f"{stop:.2f}"), Decimal(f"{target:.2f}"), 1.0,
                 "preview", now_utc(), "1h", str(f["regime"].iat[-1]), timedelta(days=5))
    ctx = MarketContext(now_utc(), live, (last_ts + timedelta(hours=1)).to_pydatetime(), 3600)
    print(f"{args.symbol} {side.value.upper()} @ {live:,.1f}  stop {stop:,.1f} ({args.atr}xATR "
          f"= {abs(live - stop) / live * 100:.2f}%)  target {target:,.1f}  regime {sig.regime}")
    for stage in RiskStage:
        d = mgr.check(sig, stage, ctx)
        print(f"\n[{stage.value}] {'ALLOWED' if d else 'BLOCKED'}")
        if d.sizing:
            print("  size:", d.sizing.line(), f"(limited by {d.sizing.limited_by})")
        for r in d.reasons:
            print("  block:", r)
        for w in d.warnings:
            print("  note: ", w)
    return 0


def cmd_kill(args, cfg, secrets) -> int:
    """Block new entries FIRST, then cancel bot orders and close bot positions at market."""
    from tradebot.execution.factory import build_execution
    mgr, _ = build_manager(cfg, secrets)
    mgr.kill_switch(args.reason, by="cli")
    print("KILL SWITCH ON: all new entries blocked. `tradebot risk resume` to clear.")
    if not args.no_flatten:
        execu = build_execution(cfg, secrets, mgr.engine.specs, mgr.store, mgr.audit)
        for line in execu.flatten_all(f"kill switch: {args.reason}") or ["no open bot positions"]:
            print("  flatten:", line)
    return 0


def cmd_resume(args, cfg, secrets) -> int:
    mgr, _ = build_manager(cfg, secrets)
    kinds = {HaltKind(k) for k in args.kinds.split(",")} if args.kinds else None
    print(f"cleared {mgr.resume('cli', kinds)} halt(s)")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("risk", help="risk engine: status, preview, kill switch")
    rs = p.add_subparsers(dest="risk_cmd", required=True)
    rs.add_parser("status").set_defaults(risk_fn=cmd_status)
    pv = rs.add_parser("preview", help="size + check a hypothetical trade at the live price")
    pv.add_argument("symbol")
    pv.add_argument("side", choices=["buy", "sell"])
    pv.add_argument("--atr", type=float, default=2.0, help="stop distance in ATR(1h)")
    pv.set_defaults(risk_fn=cmd_preview)
    k = rs.add_parser("killswitch", help="block all new entries now")
    k.add_argument("--reason", default="manual")
    k.add_argument("--no-flatten", action="store_true", help="only block new entries")
    k.set_defaults(risk_fn=cmd_kill)
    r = rs.add_parser("resume", help="clear halts (all, or --kinds kill_switch,drawdown,...)")
    r.add_argument("--kinds")
    r.set_defaults(risk_fn=cmd_resume)
    p.set_defaults(fn=lambda args: args.risk_fn(args, *load_config(args.config_dir, args.env_file)))
