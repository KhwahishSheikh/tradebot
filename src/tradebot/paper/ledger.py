"""Forward-test ledger (SQLite data/forward.db).

Records EVERY signal that passes the strategy gates on live data, including strategies that are
disabled for trading ("shadow" mode): disabled strategies still get an honest out-of-sample
track record without risking anything. Each record later gets a simulated outcome (same 5m
execution model as the backtest), which also tells us what your REJECTIONS saved or cost.
Also keeps an uptime log (one row per runtime beat) for the readiness checklist.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from tradebot.core.models import Signal
from tradebot.timeutil import now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY, bar_close TEXT NOT NULL, strategy TEXT, symbol TEXT, side TEXT,
    entry REAL, stop REAL, target REAL, max_hold_h REAL, atr_pct REAL, confidence REAL,
    regime TEXT, enabled INTEGER, proposal_id TEXT, proposal_status TEXT,
    sim_status TEXT, exit_reason TEXT, net_r REAL, evaluated_at TEXT);
CREATE TABLE IF NOT EXISTS beats (ts TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


@dataclass
class ForwardRow:
    signal_id: str
    bar_close: datetime
    symbol: str
    side: str
    entry: float
    stop: float
    target: float
    max_hold_h: float
    atr_pct: float


class ForwardLedger:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript(SCHEMA)

    # ---------- phase ----------

    def started(self) -> datetime:
        row = self.db.execute("SELECT v FROM meta WHERE k = 'paper_started'").fetchone()
        if row:
            return datetime.fromisoformat(row[0])
        t = now_utc()
        self.db.execute("INSERT INTO meta VALUES ('paper_started', ?)", (t.isoformat(),))
        return t

    # ---------- writes ----------

    def record(self, sig: Signal, bar_close: datetime, enabled: bool) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO signals (signal_id, bar_close, strategy, symbol, side, entry, stop, "
            "target, max_hold_h, atr_pct, confidence, regime, enabled) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sig.id, bar_close.isoformat(), sig.strategy, sig.instrument.symbol, sig.side.value,
             float(sig.entry), float(sig.stop_loss), float(sig.target),
             sig.max_hold.total_seconds() / 3600 if sig.max_hold else 72,
             float(sig.features.get("atr_pct") or 0), sig.confidence, sig.regime, int(enabled)))

    def link(self, signal_id: str, proposal_id: str | None, status: str) -> None:
        self.db.execute("UPDATE signals SET proposal_id = ?, proposal_status = ? WHERE signal_id = ?",
                        (proposal_id, status, signal_id))

    def sync_statuses(self, lookup) -> None:
        """Refresh proposal_status from the proposal store (decisions happen after recording)."""
        for sid, pid in self.db.execute(
                "SELECT signal_id, proposal_id FROM signals WHERE proposal_id IS NOT NULL").fetchall():
            st = lookup(pid)
            if st:
                self.db.execute("UPDATE signals SET proposal_status = ? WHERE signal_id = ?", (st, sid))

    def awaiting_outcome(self, now: datetime) -> list[ForwardRow]:
        rows = self.db.execute(
            "SELECT signal_id, bar_close, symbol, side, entry, stop, target, max_hold_h, atr_pct "
            "FROM signals WHERE sim_status IS NULL").fetchall()
        out = []
        for r in rows:
            row = ForwardRow(r[0], datetime.fromisoformat(r[1]), *r[2:])
            if (now - row.bar_close).total_seconds() / 3600 >= row.max_hold_h + 1:
                out.append(row)
        return out

    def set_outcome(self, signal_id: str, sim_status: str, exit_reason: str, net_r: float | None) -> None:
        self.db.execute("UPDATE signals SET sim_status = ?, exit_reason = ?, net_r = ?, evaluated_at = ? "
                        "WHERE signal_id = ?", (sim_status, exit_reason, net_r, now_utc().isoformat(), signal_id))

    def beat(self, at: datetime) -> None:
        """One row per 5-minute slot the runtime was alive in."""
        slot = at.replace(minute=at.minute - at.minute % 5, second=0, microsecond=0)
        self.db.execute("INSERT OR IGNORE INTO beats VALUES (?)", (slot.isoformat(),))

    # ---------- reads ----------

    def frame(self) -> pd.DataFrame:
        df = pd.read_sql_query("SELECT * FROM signals", self.db)
        if not df.empty:
            df["bar_close"] = pd.to_datetime(df["bar_close"], utc=True, format="ISO8601")
        return df

    def uptime_pct(self, since: datetime, until: datetime) -> float:
        slots = max(1, int((until - since).total_seconds() // 300))
        n = self.db.execute("SELECT count(*) FROM beats WHERE ts >= ? AND ts < ?",
                            (since.isoformat(), until.isoformat())).fetchone()[0]
        return min(100.0, 100 * n / slots)

    def close(self) -> None:
        self.db.close()
