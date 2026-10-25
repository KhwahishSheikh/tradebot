"""Shared feature frame + market regime. Built once per symbol/timeframe, used by every strategy.

Regime (crypto has no VIX, so volatility = ATR% percentile vs its own trailing history):
  HIGH_VOL      ATR% above its `high_vol_pct` percentile: all strategies off unless they opt in
  TREND_UP/DOWN ADX >= trend_adx and EMA50 vs EMA200 agree with price
  RANGING       ADX < range_adx
  NEUTRAL       everything else (weak / transitioning trend)
"""

from __future__ import annotations

from enum import StrEnum

import numpy as np
import pandas as pd

from tradebot.strategies import indicators as ta


class Regime(StrEnum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGING = "ranging"
    NEUTRAL = "neutral"
    HIGH_VOL = "high_vol"
    UNKNOWN = "unknown"   # not enough history yet


TRENDING = frozenset({Regime.TREND_UP, Regime.TREND_DOWN})


def build_features(bars: pd.DataFrame, funding: pd.DataFrame | None = None, *,
                   vol_window: int = 90 * 24, high_vol_pct: float = 0.90,
                   trend_adx: float = 25, range_adx: float = 20) -> pd.DataFrame:
    """bars: ts, open, high, low, close, volume (closed bars, ascending).
    funding: ts, close (= funding % per 8h). Aligned backward (as-of), never forward."""
    f = bars[["ts", "open", "high", "low", "close", "volume"]].reset_index(drop=True).copy()
    c = f["close"]
    for n in (20, 50, 200):
        f[f"ema{n}"] = ta.ema(c, n)
    f["atr"] = ta.atr(f, 14)
    f["atr_pct"] = f["atr"] / c
    f["rsi"] = ta.rsi(c, 14)
    f = f.join(ta.macd(c)).join(ta.adx(f, 14))
    f["vol_ma"] = f["volume"].rolling(20).mean()
    f["vol_rank"] = ta.rolling_percentile(f["atr_pct"], vol_window)

    if funding is not None and not funding.empty:
        fr = funding[["ts", "close"]].rename(columns={"close": "funding"}).sort_values("ts")
        f = pd.merge_asof(f, fr, on="ts", direction="backward")
    else:
        f["funding"] = np.nan

    up = (c > f["ema50"]) & (f["ema50"] > f["ema200"])
    down = (c < f["ema50"]) & (f["ema50"] < f["ema200"])
    regime = np.select(
        [f["ema200"].isna() | f["adx"].isna() | f["vol_rank"].isna(),
         f["vol_rank"] > high_vol_pct,
         (f["adx"] >= trend_adx) & up,
         (f["adx"] >= trend_adx) & down,
         f["adx"] < range_adx],
        [Regime.UNKNOWN, Regime.HIGH_VOL, Regime.TREND_UP, Regime.TREND_DOWN, Regime.RANGING],
        default=Regime.NEUTRAL)
    f["regime"] = pd.Series(regime, index=f.index).astype(str)
    return f
