"""Local market-data store (DuckDB, one file). All timestamps stored in UTC; convert on read.

Series naming follows Delta's convention so every series is just "candles":
  BTCUSD, MARK:BTCUSD, FUNDING:BTCUSD (funding % per 8h), OI:BTCUSD, NIFTY, INDIA VIX, RELIANCE
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from tradebot.core.models import Market
from tradebot.timeutil import IST, now_utc

CANDLE_COLS = ["ts", "open", "high", "low", "close", "volume", "oi"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    market VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    resolution VARCHAR NOT NULL,
    ts TIMESTAMPTZ NOT NULL,          -- bar OPEN time, UTC
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume DOUBLE, oi DOUBLE,
    ingested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (market, symbol, resolution, ts)
);
CREATE TABLE IF NOT EXISTS snapshots (
    market VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,            -- ticker | orderbook | quote
    ts TIMESTAMPTZ NOT NULL,
    payload JSON NOT NULL
);
"""


class MarketStore:
    def __init__(self, path: Path | str):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect(str(path))
        self.db.execute("SET TimeZone = 'UTC'")
        self.db.execute(SCHEMA)

    def upsert_candles(self, market: Market, symbol: str, resolution: str, df: pd.DataFrame) -> int:
        """df columns: ts (tz-aware), open, high, low, close, volume[, oi]. Re-ingesting a bar replaces it
        (the latest, still-forming bar gets corrected on the next run)."""
        if df.empty:
            return 0
        df = df.copy()
        if "oi" not in df:
            df["oi"] = None
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df[CANDLE_COLS].drop_duplicates("ts", keep="last")
        df.insert(0, "resolution", resolution)
        df.insert(0, "symbol", symbol)
        df.insert(0, "market", market.value)
        df["ingested_at"] = now_utc()
        self.db.register("_incoming", df)
        try:
            self.db.execute("INSERT OR REPLACE INTO candles SELECT * FROM _incoming")
        finally:
            self.db.unregister("_incoming")
        return len(df)

    def candles(self, market: Market, symbol: str, resolution: str,
                start: datetime | None = None, end: datetime | None = None,
                tz: str | None = None) -> pd.DataFrame:
        """Bars with start <= ts < end, sorted. tz='IST' converts the ts column for display."""
        q = ("SELECT ts, open, high, low, close, volume, oi FROM candles "
             "WHERE market = ? AND symbol = ? AND resolution = ?")
        args: list[Any] = [market.value, symbol, resolution]
        if start:
            q += " AND ts >= ?"
            args.append(start)
        if end:
            q += " AND ts < ?"
            args.append(end)
        df = self.db.execute(q + " ORDER BY ts", args).df()
        if not df.empty:
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
            if tz == "IST":
                df["ts"] = df["ts"].dt.tz_convert(IST)
        return df

    def first_ts(self, market: Market, symbol: str, resolution: str) -> datetime | None:
        row = self.db.execute(
            "SELECT epoch_ms(min(ts)) FROM candles WHERE market = ? AND symbol = ? AND resolution = ?",
            [market.value, symbol, resolution]).fetchone()
        return datetime.fromtimestamp(row[0] / 1000, UTC) if row and row[0] is not None else None

    def last_ts(self, market: Market, symbol: str, resolution: str) -> datetime | None:
        row = self.db.execute(
            "SELECT epoch_ms(max(ts)) FROM candles WHERE market = ? AND symbol = ? AND resolution = ?",
            [market.value, symbol, resolution]).fetchone()
        return datetime.fromtimestamp(row[0] / 1000, UTC) if row and row[0] is not None else None

    def add_snapshot(self, market: Market, symbol: str, kind: str, payload: dict) -> None:
        self.db.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)",
                        [market.value, symbol, kind, now_utc(), json.dumps(payload, default=str)])

    def summary(self) -> pd.DataFrame:
        return self.db.execute(
            "SELECT market, symbol, resolution, count(*) AS bars, min(ts) AS first, max(ts) AS last "
            "FROM candles GROUP BY ALL ORDER BY ALL").df()

    def close(self) -> None:
        self.db.close()
