"""`tradebot backtest run|wfo`: reports land in reports/<timestamp>-<kind>/."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from tradebot.backtest.portfolio import run_portfolio
from tradebot.backtest.runner import (
    build_context,
    data_window,
    evaluate,
    run_backtest,
    write_report,
)
from tradebot.backtest.walkforward import picks_table, walk_forward
from tradebot.config import load_config
from tradebot.data.store import MarketStore

NOTES = [
    "Signals at 1h bar close; fill at the first 5m open after the approval delay (+slippage)",
    "Stops/targets walked on 5m bars; stop assumed first when both inside one bar",
    "Taker fee+GST on entry/stop/time exits, maker fee+GST on target exits, funding at settlements",
    "Every trade passes the live CryptoRiskEngine twice (proposal + execution) incl. quiet hours",
    "Risk checks use realised equity; reported curve is marked to market daily",
    "No historical event blackouts (FOMC/CPI) and no news filter: neither has history yet",
    "Tax lines are rough estimates under two interpretations; not tax advice",
]


def _common(args):
    cfg, _ = load_config(args.config_dir, args.env_file)
    from tradebot.data.cli import _delta
    from tradebot.risk.sizing import load_specs
    specs = load_specs(_delta(cfg, _), cfg.crypto.symbols, cfg.data.db_path.parent / "delta_specs.json")
    store = MarketStore(cfg.data.db_path)
    ctx = build_context(cfg, store, specs, ignore_quiet_hours=args.ignore_quiet_hours)
    entries = [e for e in ctx.cfg.strategies.crypto if e.enabled and (
        not args.strategies or e.name in args.strategies.split(","))]
    lo, hi = data_window(ctx)
    start = max(lo, datetime.fromisoformat(args.start).replace(tzinfo=UTC)) if args.start else lo
    end = min(hi, datetime.fromisoformat(args.end).replace(tzinfo=UTC)) if args.end else hi
    return ctx, entries, start, end, timedelta(minutes=args.delay)


def _print(r: dict) -> None:
    pd.set_option("display.width", 200)
    print("equity :", r["curve"])
    print("BTC B&H:", r["benchmark_btc"])
    print("trades :", r["trades"])
    print("tax    :", r["tax"])
    for k in ("by_strategy", "by_regime", "blocked"):
        if len(r[k]):
            print(f"\n{k}:\n{r[k].to_string(index=False)}")


def cmd_run(args) -> int:
    ctx, entries, start, end, delay = _common(args)
    print(f"backtest {start:%Y-%m-%d} -> {end:%Y-%m-%d}, {[e.name for e in entries]}, "
          f"approval delay {args.delay}m")
    _, r = run_backtest(ctx, entries, start, end, delay)
    _print(r)
    out = Path("reports") / f"{datetime.now():%Y%m%d-%H%M}-backtest"
    print("\nreport:", write_report(out, "Backtest (configured params)", r,
                                    NOTES + [f"Approval delay {args.delay} min"]))
    return 0


def cmd_wfo(args) -> int:
    ctx, entries, start, end, delay = _common(args)
    print(f"walk-forward {start:%Y-%m-%d} -> {end:%Y-%m-%d}, train {args.train}d / test {args.test}d")
    oos, picks, folds = walk_forward(ctx, entries, start, end, delay, args.train, args.test)
    if not folds:
        print("not enough data for one fold")
        return 1
    oos_start, oos_end = folds[0].test[0], folds[-1].test[1]
    res = run_portfolio(ctx.cfg, ctx.risk, oos, ctx.exec_data, delay)
    r = evaluate(ctx, res, oos_start, oos_end)
    print(f"\n=== OUT-OF-SAMPLE portfolio {oos_start:%Y-%m-%d} -> {oos_end:%Y-%m-%d} "
          f"({len(folds)} folds) ===")
    _print(r)
    _, base = run_backtest(ctx, entries, oos_start, oos_end, delay)
    print("\n=== same period, configured default params (no optimisation) ===")
    print("equity :", base["curve"])
    print("trades :", base["trades"])
    pt = picks_table(picks)
    print("\nper-fold picks (IS = in-sample train, OOS = unseen test):\n" + pt.to_string(index=False))
    out = Path("reports") / f"{datetime.now():%Y%m%d-%H%M}-wfo"
    path = write_report(out, "Walk-forward, out-of-sample only", r, NOTES + [
        f"Train {args.train}d / test {args.test}d, {len(folds)} folds, approval delay {args.delay}m",
        f"Default-params baseline on the same period: {base['curve']} / {base['trades']}"])
    pt.to_csv(out / "fold_picks.csv", index=False)
    with path.open("a") as fh:
        fh.write("\n\n## Per-fold parameter picks\n\n" + pt.to_markdown(index=False) + "\n")
    print("\nreport:", path)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("backtest", help="cost-aware backtests + walk-forward")
    bs = p.add_subparsers(dest="bt_cmd", required=True)
    for name, fn in (("run", cmd_run), ("wfo", cmd_wfo)):
        q = bs.add_parser(name)
        q.add_argument("--strategies")
        q.add_argument("--start")
        q.add_argument("--end")
        q.add_argument("--delay", type=int, default=3, help="approval delay, minutes")
        q.add_argument("--ignore-quiet-hours", action="store_true")
        if name == "wfo":
            q.add_argument("--train", type=int, default=180)
            q.add_argument("--test", type=int, default=60)
        q.set_defaults(fn=fn)
