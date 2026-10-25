"""Technical indicators. Every function is CAUSAL: row i depends only on rows <= i.
(tests/test_strategies.py::test_features_are_causal enforces this.)"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
                     axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return _wilder(true_range(df), n)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up, down = _wilder(d.clip(lower=0), n), _wilder(-d.clip(upper=0), n)
    return 100 - 100 / (1 + up / down.replace(0, np.nan))


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def adx(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = _wilder(true_range(df), n)
    plus_di = 100 * _wilder(plus_dm, n) / tr
    minus_di = 100 * _wilder(minus_dm, n) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return pd.DataFrame({"adx": _wilder(dx, n), "plus_di": plus_di, "minus_di": minus_di})


def donchian(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Channel of the PREVIOUS n bars (excludes the current bar, so a breakout is detectable)."""
    return pd.DataFrame({"dc_high": df["high"].rolling(n).max().shift(1),
                         "dc_low": df["low"].rolling(n).min().shift(1)})


def session_vwap(df: pd.DataFrame, anchor: str = "1D") -> pd.Series:
    """VWAP reset every `anchor` period (UTC days for crypto)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    key = df["ts"].dt.floor(anchor)
    pv = (tp * df["volume"]).groupby(key).cumsum()
    v = df["volume"].groupby(key).cumsum()
    return pv / v.replace(0, np.nan)


def rolling_percentile(s: pd.Series, window: int) -> pd.Series:
    """Percentile rank (0..1) of each value within its trailing window."""
    return s.rolling(window, min_periods=window // 4).rank(pct=True)
