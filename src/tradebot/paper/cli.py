"""`tradebot paper report|evaluate`."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from tradebot.config import load_config


def _stores(cfg):
    from tradebot.execution.orders import ExecutionStore
    from tradebot.paper.ledger import ForwardLedger
    from tradebot.proposals.store import ProposalStore
    from tradebot.risk.state import RiskStateStore
    d = cfg.data.db_path.parent
    return (ForwardLedger(cfg.paper.ledger_path), ProposalStore(d / "proposals.db"),
            ExecutionStore(cfg.execution.db_path), RiskStateStore(d / "risk_state.db", cfg.account.capital_inr))


def cmd_report(args, cfg, secrets) -> int:
    from tradebot.paper.report import build_report
    ledger, props, execs, risk = _stores(cfg)
    bt = None
    if not args.no_backtest:
        from tradebot.backtest.runner import build_context, run_backtest
        from tradebot.data.store import MarketStore
        from tradebot.risk.sizing import ContractSpec
        specs = {k: ContractSpec(**v) for k, v in json.loads(
            (cfg.data.db_path.parent / "delta_specs.json").read_text()).items()}
        ctx = build_context(cfg, MarketStore(cfg.data.db_path), specs)
        entries = [e for e in cfg.strategies.crypto if e.enabled]

        def bt(start, end):
            if (end - start) < timedelta(hours=2):
                return {"trades": {"trades": 0}}
            return run_backtest(ctx, entries, start, end, timedelta(minutes=3))[1]
    text, checks = build_report(cfg, ledger, props, execs, risk, bt)
    print(text)
    out = Path("reports") / f"{datetime.now():%Y%m%d-%H%M}-paper.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(text)
    print(f"\nsaved {out}")
    return 0 if all(c.ok for c in checks) else 1


def cmd_evaluate(args, cfg, secrets) -> int:
    from tradebot.data.store import MarketStore
    from tradebot.paper.evaluate import evaluate_outcomes
    ledger, *_ = _stores(cfg)
    print(f"evaluated {evaluate_outcomes(cfg, ledger, MarketStore(cfg.data.db_path))} signals")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("paper", help="paper-trading phase: report + readiness checklist")
    ps = p.add_subparsers(dest="pp_cmd", required=True)
    r = ps.add_parser("report")
    r.add_argument("--no-backtest", action="store_true")
    r.set_defaults(pp_fn=cmd_report)
    ps.add_parser("evaluate", help="score matured forward signals").set_defaults(pp_fn=cmd_evaluate)
    p.set_defaults(fn=lambda args: args.pp_fn(args, *load_config(args.config_dir, args.env_file)))
