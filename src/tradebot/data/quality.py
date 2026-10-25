"""Gap detection against the expected bar grid, plus OHLC sanity checks.

Gaps are reported, never silently filled in storage. Fill (or drop) at read time per use case:
a backtest must not trade on a forward-filled price that never existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd

from tradebot.core.models import Market
from tradebot.timeutil import IST
from tradebot.timing.nse import MARKET_CLOSE, MARKET_OPEN, NseCalendar

RESOLUTION_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "10m": 600, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400, "1w": 604800,
}


def expected_grid(market: Market, resolution: str, start: datetime, end: datetime,
                  nse: NseCalendar | None = None) -> pd.DatetimeIndex:
    """Bar open-times (UTC) that should exist in [start, end)."""
    step = timedelta(seconds=RESOLUTION_SECONDS[resolution])
    if market is Market.CRYPTO_DELTA:
        s = pd.Timestamp(start).tz_convert(UTC).floor(step)
        if s < pd.Timestamp(start):
            s += step
        return pd.date_range(s, pd.Timestamp(end), freq=step, inclusive="left")

    if nse is None:
        raise ValueError("NSE grid needs the NSE calendar")
    out: list[datetime] = []
    day = start.astimezone(IST).date()
    while day <= end.astimezone(IST).date():
        if nse.is_trading_day(day):
            open_t, close_t = nse.session_bounds(day) or (MARKET_OPEN, MARKET_CLOSE)
            if resolution == "1d":
                out.append(datetime.combine(day, datetime.min.time(), IST))
            else:
                t = datetime.combine(day, open_t, IST)
                close = datetime.combine(day, close_t, IST)
                while t < close:
                    out.append(t)
                    t += step
        day += timedelta(days=1)
    idx = pd.DatetimeIndex(out).tz_convert(UTC) if out else pd.DatetimeIndex([], tz=UTC)
    return idx[(idx >= pd.Timestamp(start)) & (idx < pd.Timestamp(end))]


@dataclass(frozen=True)
class Gap:
    start: pd.Timestamp
    end: pd.Timestamp     # last missing bar (inclusive)
    bars: int


def find_gaps(ts: pd.Series, expected: pd.DatetimeIndex) -> list[Gap]:
    have = pd.DatetimeIndex(pd.to_datetime(ts, utc=True))
    missing = expected.difference(have)
    if missing.empty:
        return []
    pos = expected.get_indexer(missing)
    gaps, run_start = [], 0
    for i in range(1, len(pos) + 1):
        if i == len(pos) or pos[i] != pos[i - 1] + 1:
            gaps.append(Gap(missing[run_start], missing[i - 1], i - run_start))
            run_start = i
    return gaps


def ohlc_issues(df: pd.DataFrame) -> list[str]:
    issues = []
    if df.empty:
        return issues
    if df["ts"].duplicated().any():
        issues.append(f"{int(df['ts'].duplicated().sum())} duplicate timestamps")
    nan = df[["open", "high", "low", "close"]].isna().any(axis=1)
    if nan.any():
        issues.append(f"{int(nan.sum())} bars with missing OHLC")
    ok = df[~nan]
    bad = (ok["high"] < ok["low"]) | (ok[["open", "close"]].max(axis=1) > ok["high"]) | (
        ok[["open", "close"]].min(axis=1) < ok["low"])
    if bad.any():
        issues.append(f"{int(bad.sum())} bars violate low <= open/close <= high")
    if (df["volume"].fillna(0) < 0).any():
        issues.append("negative volume")
    return issues
