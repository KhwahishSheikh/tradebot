"""Persistent risk state (SQLite): equity marks, closed trades, halts.

Survives restarts on purpose: a crash must not reset the daily loss counter or clear a halt.
Halts:
  kill_switch  manual; only `resume` clears it
  drawdown     max drawdown hit; only `resume` clears it
  daily_loss   auto-expires at the next IST midnight
  cooldown     consecutive losses; auto-expires after loss_cooldown_hours
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from tradebot.timeutil import IST, now_utc


class HaltKind(StrEnum):
    KILL_SWITCH = "kill_switch"
    DRAWDOWN = "drawdown"
    DAILY_LOSS = "daily_loss"
    COOLDOWN = "cooldown"
    RECONCILE = "reconcile"     # bot and exchange disagree: manual check needed
    PAUSE = "pause"             # you said /pause
    SAFE_MODE = "safe_mode"     # runtime unhealthy (stale data, exchange down): auto-cleared


MANUAL_ONLY = {HaltKind.KILL_SWITCH, HaltKind.DRAWDOWN, HaltKind.RECONCILE, HaltKind.PAUSE}


@dataclass(frozen=True)
class Halt:
    id: int
    kind: HaltKind
    reason: str
    created_at: datetime
    until: datetime | None


@dataclass(frozen=True)
class OpenRisk:
    """One open position as the risk engine sees it (filled by execution in Ticket 9)."""
    symbol: str
    side: str
    contracts: int
    entry: float
    stop: float
    contract_value: float

    @property
    def risk_usd(self) -> float:
        return abs(self.entry - self.stop) * self.contracts * self.contract_value

    @property
    def notional_usd(self) -> float:
        return self.entry * self.contracts * self.contract_value


@dataclass
class PortfolioState:
    equity_inr: float
    day_start_equity_inr: float
    peak_equity_inr: float
    open_positions: list[OpenRisk] = field(default_factory=list)
    recent_results: list[tuple[datetime, float]] = field(default_factory=list)  # (closed_at, pnl)
    halts: list[Halt] = field(default_factory=list)

    @property
    def drawdown_pct(self) -> float:
        return max(0.0, (self.peak_equity_inr - self.equity_inr) / self.peak_equity_inr * 100)

    @property
    def day_pnl_pct(self) -> float:
        return (self.equity_inr - self.day_start_equity_inr) / self.day_start_equity_inr * 100

    def consecutive_losses(self) -> tuple[int, datetime | None]:
        n, last = 0, None
        for ts, pnl in sorted(self.recent_results, reverse=True):
            if pnl >= 0:
                break
            n += 1
            last = last or ts
        return n, last


SCHEMA = """
CREATE TABLE IF NOT EXISTS equity_marks (ts TEXT NOT NULL, equity_inr REAL NOT NULL);
CREATE TABLE IF NOT EXISTS closed_trades (
    ts TEXT NOT NULL, symbol TEXT, strategy TEXT, pnl_inr REAL NOT NULL, r_multiple REAL);
CREATE TABLE IF NOT EXISTS halts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, reason TEXT NOT NULL,
    created_at TEXT NOT NULL, until TEXT, cleared_at TEXT, cleared_by TEXT);
"""


def ist_midnight_after(t: datetime) -> datetime:
    d = t.astimezone(IST).date() + timedelta(days=1)
    return datetime.combine(d, datetime.min.time(), IST)


class RiskStateStore:
    def __init__(self, path: Path | str, starting_equity_inr: float):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)  # autocommit
        self.db.executescript(SCHEMA)
        self.starting_equity = starting_equity_inr

    # ---------- writes ----------

    def mark_equity(self, equity_inr: float, at: datetime | None = None) -> None:
        self.db.execute("INSERT INTO equity_marks VALUES (?, ?)",
                        ((at or now_utc()).isoformat(), equity_inr))

    def record_trade(self, pnl_inr: float, symbol: str = "", strategy: str = "",
                     r_multiple: float | None = None, at: datetime | None = None) -> None:
        self.db.execute("INSERT INTO closed_trades VALUES (?, ?, ?, ?, ?)",
                        ((at or now_utc()).isoformat(), symbol, strategy, pnl_inr, r_multiple))

    def halt(self, kind: HaltKind, reason: str, until: datetime | None = None,
             at: datetime | None = None) -> None:
        if any(h.kind is kind for h in self.active_halts(at)):
            return  # already halted for this reason; don't spam
        self.db.execute("INSERT INTO halts (kind, reason, created_at, until) VALUES (?, ?, ?, ?)",
                        (kind.value, reason, (at or now_utc()).isoformat(),
                         until.isoformat() if until else None))

    def resume(self, by: str, kinds: set[HaltKind] | None = None) -> int:
        kinds = kinds or set(HaltKind)
        q = ",".join("?" * len(kinds))
        cur = self.db.execute(
            f"UPDATE halts SET cleared_at = ?, cleared_by = ? WHERE cleared_at IS NULL AND kind IN ({q})",
            (now_utc().isoformat(), by, *[k.value for k in kinds]))
        return cur.rowcount

    # ---------- reads ----------

    def active_halts(self, at: datetime | None = None) -> list[Halt]:
        at = at or now_utc()
        rows = self.db.execute(
            "SELECT id, kind, reason, created_at, until FROM halts WHERE cleared_at IS NULL").fetchall()
        out = []
        for id_, kind, reason, created, until in rows:
            u = datetime.fromisoformat(until) if until else None
            if u is None or u > at:
                out.append(Halt(id_, HaltKind(kind), reason, datetime.fromisoformat(created), u))
        return out

    def state(self, open_positions: list[OpenRisk] | None = None,
              at: datetime | None = None) -> PortfolioState:
        at = at or now_utc()
        marks = [(datetime.fromisoformat(t), e) for t, e in self.db.execute(
            "SELECT ts, equity_inr FROM equity_marks WHERE ts <= ? ORDER BY ts", (at.isoformat(),))]
        equity = marks[-1][1] if marks else self.starting_equity
        day_start = ist_midnight_after(at) - timedelta(days=1)
        before = [e for t, e in marks if t < day_start]
        # no mark before today: the day started at the starting equity (not at today's first mark,
        # which would hide today's first loss from the daily-loss limit)
        day_start_equity = before[-1] if before else self.starting_equity
        peak = max([self.starting_equity] + [e for _, e in marks])
        results = [(datetime.fromisoformat(t), p) for t, p in self.db.execute(
            "SELECT ts, pnl_inr FROM closed_trades ORDER BY ts DESC LIMIT 20")]
        return PortfolioState(equity, day_start_equity, peak, open_positions or [], results,
                              self.active_halts(at))

    def close(self) -> None:
        self.db.close()
