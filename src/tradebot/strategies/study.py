"""Strategy comparison study (an event study, NOT the Ticket 7 backtester).

For every setup a strategy would have produced on historical bars:
  entry at the NEXT bar's open (no peeking at the signal bar's close fill),
  exit at stop / target / max-hold, whichever first; if stop and target are both inside one bar,
  assume the stop hit first (pessimistic); gaps through the stop fill at the open.
Costs: round-trip fees+GST+slippage and actual funding paid over the hold.
One open trade per strategy+symbol at a time. No approval delay, no sizing, no portfolio:
those come in Ticket 7.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from tradebot.core.models import Side
from tradebot.data.quality import RESOLUTION_SECONDS
from tradebot.strategies.base import Strategy
from tradebot.strategies.engine import SignalEngine


@dataclass
class Trade:
    strategy: str
    symbol: str
    side: str
    regime: str
    passed: bool
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    bars: int
    exit_reason: str
    gross_r: float
    fee_r: float
    funding_r: float
    confidence: float

    @property
    def net_r(self) -> float:
        return self.gross_r - self.fee_r - self.funding_r


def simulate(f: pd.DataFrame, i: int, side: Side, stop_dist: float, target_dist: float,
             max_bars: int) -> tuple[int, float, str, float]:
    """Returns (exit_index, exit_price, reason, entry_price). Entry at open of i+1."""
    j0 = i + 1
    entry = f["open"].iat[j0]
    long_ = side is Side.BUY
    stop = entry - stop_dist if long_ else entry + stop_dist
    target = entry + target_dist if long_ else entry - target_dist
    last = min(j0 + max_bars - 1, len(f) - 1)
    o, h, lo, c = (f[k].to_numpy() for k in ("open", "high", "low", "close"))
    for j in range(j0, last + 1):
        if long_:
            if j > j0 and o[j] <= stop:
                return j, o[j], "stop(gap)", entry
            if lo[j] <= stop:
                return j, stop, "stop", entry
            if h[j] >= target:
                return j, max(target, o[j]) if j > j0 else target, "target", entry
        else:
            if j > j0 and o[j] >= stop:
                return j, o[j], "stop(gap)", entry
            if h[j] >= stop:
                return j, stop, "stop", entry
            if lo[j] <= target:
                return j, min(target, o[j]) if j > j0 else target, "target", entry
    return last, c[last], "time", entry


def study(engine: SignalEngine, strat: Strategy, symbol: str) -> list[Trade]:
    f = strat.prepare(engine.features(symbol, strat.timeframe))
    bar_hours = RESOLUTION_SECONDS[strat.timeframe] / 3600
    trades: list[Trade] = []
    busy_until = -1
    for i in range(strat.warmup, len(f) - 2):
        if i <= busy_until:
            continue
        ev = engine.evaluate(strat, f, i, symbol)
        if ev is None:
            continue
        s = ev.setup
        j, px, why, entry = simulate(f, i, s.side, s.risk, abs(s.target - s.entry),
                                     strat.max_hold_bars)
        sgn = 1 if s.side is Side.BUY else -1
        gross_r = sgn * (px - entry) / s.risk
        fee_r = engine.round_trip_pct / 100 * entry / s.risk
        held = f["funding"].iloc[i + 1:j + 1].fillna(0).to_numpy()
        funding_r = sgn * float(np.sum(held)) * bar_hours / 8 / 100 * entry / s.risk
        trades.append(Trade(strat.name, symbol, s.side.value, ev.regime, ev.passed,
                            f["ts"].iat[i + 1], f["ts"].iat[j], j - i, why,
                            gross_r, fee_r, funding_r, ev.confidence))
        busy_until = j
    return trades


def summarize(trades: list[Trade], by: list[str]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame([{**t.__dict__, "net_r": t.net_r} for t in trades])

    def stats(g: pd.DataFrame) -> pd.Series:
        r = g["net_r"]
        wins, losses = r[r > 0].sum(), -r[r < 0].sum()
        eq = r.cumsum()
        streak = (r <= 0).astype(int)
        max_streak = int(streak.groupby((streak == 0).cumsum()).sum().max())
        months = max((g["exit_ts"].max() - g["entry_ts"].min()).days / 30.4, 1)
        return pd.Series({
            "trades": len(g), "per_month": round(len(g) / months, 1),
            "win%": round(100 * (r > 0).mean(), 1),
            "avg_R_gross": round(g["gross_r"].mean(), 2), "avg_cost_R": round(
                (g["fee_r"] + g["funding_r"]).mean(), 2),
            "avg_R_net": round(r.mean(), 2), "total_R": round(r.sum(), 1),
            "PF": round(wins / losses, 2) if losses else np.inf,
            "maxDD_R": round((eq.cummax() - eq).max(), 1), "max_loss_streak": max_streak,
            "avg_bars": round(g["bars"].mean(), 1)})

    return df.groupby(by).apply(stats, include_groups=False).reset_index()
