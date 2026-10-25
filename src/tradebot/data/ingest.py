"""Incremental backfill: fetch -> validate -> upsert -> gap report. Safe to re-run any time."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from tradebot.core.interfaces import MarketData
from tradebot.data.quality import RESOLUTION_SECONDS, Gap, expected_grid, find_gaps, ohlc_issues
from tradebot.data.store import MarketStore
from tradebot.timeutil import now_utc
from tradebot.timing.nse import NseCalendar

log = logging.getLogger(__name__)


def frames_empty() -> pd.DataFrame:
    return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])


@dataclass
class IngestReport:
    symbol: str
    resolution: str
    start: datetime
    end: datetime
    rows: int = 0
    gaps: list[Gap] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def missing_bars(self) -> int:
        return sum(g.bars for g in self.gaps)

    def line(self) -> str:
        s = (f"{self.symbol:<16} {self.resolution:<4} {self.rows:>6} bars  "
             f"{self.start:%Y-%m-%d %H:%M} -> {self.end:%Y-%m-%d %H:%M} UTC  "
             f"gaps: {len(self.gaps)} ({self.missing_bars} bars)")
        return s + ("  ISSUES: " + "; ".join(self.issues) if self.issues else "")


class Ingestor:
    def __init__(self, store: MarketStore, nse: NseCalendar | None = None):
        self.store = store
        self.nse = nse

    def backfill(self, source: MarketData, symbol: str, resolution: str,
                 lookback_days: int, end: datetime | None = None,
                 check_gaps: bool = True) -> IngestReport:
        """Fill [now - lookback, end]: older history missing before the first stored bar is
        fetched (backward fill), then everything from the last stored bar forward. The last
        stored bar is re-fetched because it may have been incomplete when first saved."""
        end = end or now_utc()
        step = timedelta(seconds=RESOLUTION_SECONDS[resolution])
        window_start = pd.Timestamp(end - timedelta(days=lookback_days)).floor(step).to_pydatetime()
        start = window_start
        first = self.store.first_ts(source.market, symbol, resolution)
        last = self.store.last_ts(source.market, symbol, resolution)
        frames = []
        if first and last and first > window_start:
            frames.append(source.candles(symbol, resolution, window_start, first))
        if last and last > start:
            start = last
        rep = IngestReport(symbol, resolution, window_start if frames else start, end)

        frames.append(source.candles(symbol, resolution, start, end))
        frames = [f for f in frames if not f.empty]
        df = pd.concat(frames, ignore_index=True) if frames else frames_empty()
        # drop the still-forming bar: it would be stored with partial OHLC
        if not df.empty:
            df = df[df["ts"] + step <= end]
        rep.issues = ohlc_issues(df)
        if rep.issues:
            log.warning("%s %s data issues: %s", symbol, resolution, rep.issues)
        rep.rows = self.store.upsert_candles(source.market, symbol, resolution, df)

        if check_gaps and ":" not in symbol:  # FUNDING:/OI: series are sparse by nature
            window_start = end - timedelta(days=lookback_days)
            stored = self.store.candles(source.market, symbol, resolution, window_start, end)
            grid = expected_grid(source.market, resolution, window_start, end - step, self.nse)
            first = stored["ts"].min() if not stored.empty else None
            if first is not None:
                grid = grid[grid >= first]  # before listing / before history is not a gap
            rep.gaps = find_gaps(stored["ts"], grid)
        return rep
