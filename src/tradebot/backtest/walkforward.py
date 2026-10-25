"""Walk-forward optimisation: the only results worth believing are out-of-sample.

For each fold: pick each strategy's params on the TRAIN window (sum of net R, min trades),
then trade them unseen on the following TEST window. Stitch all TEST windows together and run
the full portfolio + risk engine over them. Also report in-sample vs out-of-sample R per fold
(the gap = how much the optimiser was fitting noise) and how stable the chosen params are.
Grids are deliberately small: every extra combination is another chance to overfit.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from tradebot.backtest.execution import simulate_trade
from tradebot.backtest.portfolio import SignalEvent
from tradebot.backtest.runner import Context, generate_events
from tradebot.config import StrategyEntry

GRIDS: dict[str, dict[str, list[float]]] = {
    "ema_trend": {"min_adx": [20, 25], "stop_atr": [1.5, 2.0, 2.5], "rr": [1.5, 2.0, 3.0]},
    "donchian_breakout": {"lookback": [24, 48, 96], "stop_atr": [2.0, 2.5, 3.0], "rr": [2.0, 3.0]},
    "rsi_pullback": {"dip": [35, 40], "stop_atr": [1.5, 2.0], "rr": [1.5, 2.0, 2.5]},
    "vwap_intraday": {"stop_atr": [1.5, 2.0], "rr": [1.5, 2.0]},
}


@dataclass
class Fold:
    train: tuple[datetime, datetime]
    test: tuple[datetime, datetime]


@dataclass
class FoldPick:
    strategy: str
    fold: int
    params: dict
    is_r: float          # in-sample sum net R of the pick
    is_trades: int
    oos_r: float         # same params on the test window
    oos_trades: int


def make_folds(start: datetime, end: datetime, train_days: int, test_days: int) -> list[Fold]:
    folds, t = [], start
    while t + timedelta(days=train_days + test_days) <= end + timedelta(days=1):
        tr = (t, t + timedelta(days=train_days))
        folds.append(Fold(tr, (tr[1], min(tr[1] + timedelta(days=test_days), end))))
        t += timedelta(days=test_days)
    return folds


def combos(name: str) -> list[dict]:
    g = GRIDS.get(name, {})
    keys = list(g)
    return [dict(zip(keys, vals, strict=True)) for vals in itertools.product(*g.values())] or [{}]


def r_series(ctx: Context, events: list[SignalEvent], delay: timedelta) -> pd.DataFrame:
    """Isolated per-strategy R outcomes (one position per symbol at a time), costs included."""
    rows, busy = [], defaultdict(lambda: pd.Timestamp.min.tz_localize("UTC"))
    for ev in sorted(events, key=lambda e: e.bar_close):
        s = ev.signal
        sym = s.instrument.symbol
        if pd.Timestamp(ev.bar_close) < busy[sym]:
            continue
        sim = simulate_trade(ctx.exec_data[sym], s.side, ev.bar_close, float(s.entry),
                             float(s.stop_loss), float(s.target), s.max_hold, ctx.cfg.costs.crypto,
                             delay, ctx.cfg.risk.max_price_drift_pct)
        if sim.status != "filled":
            continue
        busy[sym] = pd.Timestamp(sim.exit_ts)
        rows.append({"ts": pd.Timestamp(ev.bar_close), "r": sim.net_r})
    return pd.DataFrame(rows, columns=["ts", "r"])


def walk_forward(ctx: Context, entries: list[StrategyEntry], start: datetime, end: datetime,
                 delay: timedelta, train_days: int = 180, test_days: int = 60,
                 min_trades: int = 6, progress=print) -> tuple[list[SignalEvent], list[FoldPick], list[Fold]]:
    folds = make_folds(start, end, train_days, test_days)
    oos_events: list[SignalEvent] = []
    picks: list[FoldPick] = []
    for entry in entries:
        cands = combos(entry.name)
        progress(f"  {entry.name}: {len(cands)} param sets x {len(entry.symbols)} symbols")
        cache = {}
        for c in cands:
            e = entry.model_copy(update={"params": {**entry.params, **c}})
            evs = generate_events(ctx, e, start, end)
            cache[tuple(sorted(c.items()))] = (evs, r_series(ctx, evs, delay))
        default_key = tuple(sorted({k: v for k, v in entry.params.items()}.items()))
        for fi, f in enumerate(folds):
            best, best_score = None, float("-inf")
            for key, (_, rs) in cache.items():
                tr = rs[(rs["ts"] >= pd.Timestamp(f.train[0])) & (rs["ts"] < pd.Timestamp(f.train[1]))]
                if len(tr) >= min_trades and tr["r"].sum() > best_score:
                    best, best_score = key, tr["r"].sum()
            if best is None:  # nothing qualified: fall back to configured defaults
                best = default_key if default_key in cache else next(iter(cache))
            evs, rs = cache[best]
            tr = rs[(rs["ts"] >= pd.Timestamp(f.train[0])) & (rs["ts"] < pd.Timestamp(f.train[1]))]
            te = rs[(rs["ts"] >= pd.Timestamp(f.test[0])) & (rs["ts"] < pd.Timestamp(f.test[1]))]
            picks.append(FoldPick(entry.name, fi, dict(best), round(tr["r"].sum(), 2), len(tr),
                                  round(te["r"].sum(), 2), len(te)))
            oos_events += [e for e in evs if f.test[0] <= e.bar_close < f.test[1]]
    return oos_events, picks, folds


def picks_table(picks: list[FoldPick]) -> pd.DataFrame:
    df = pd.DataFrame([p.__dict__ for p in picks])
    if df.empty:
        return df
    df["params"] = df["params"].map(lambda d: ", ".join(f"{k}={v:g}" for k, v in d.items()) or "defaults")
    df["is_R_per_trade"] = (df["is_r"] / df["is_trades"].clip(lower=1)).round(2)
    df["oos_R_per_trade"] = (df["oos_r"] / df["oos_trades"].clip(lower=1)).round(2)
    return df[["strategy", "fold", "params", "is_trades", "is_r", "is_R_per_trade",
               "oos_trades", "oos_r", "oos_R_per_trade"]]
