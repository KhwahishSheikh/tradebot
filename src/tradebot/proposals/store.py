"""Proposal persistence (SQLite). A restart must never lose a pending proposal's expiry or let
an old approval be replayed: status + expiry live on disk, and every change is versioned."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tradebot.core.models import Horizon, Instrument, Market, ProposalStatus, Side, Signal
from tradebot.proposals.model import Proposal

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY, status TEXT NOT NULL, symbol TEXT NOT NULL, strategy TEXT NOT NULL,
    side TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    body TEXT NOT NULL, signal TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_status ON proposals(status);
"""


def _dump(p: Proposal) -> str:
    d = p.as_dict()
    for k in ("created_at", "expires_at", "decided_at"):
        d[k] = d[k].isoformat() if d[k] else None
    return json.dumps(d, default=str)


def signal_to_json(s: Signal) -> str:
    i = s.instrument
    return json.dumps({
        "instrument": {"market": i.market.value, "symbol": i.symbol, "exchange_token": i.exchange_token,
                       "lot_size": str(i.lot_size), "tick_size": str(i.tick_size)},
        "side": s.side.value, "strategy": s.strategy, "horizon": s.horizon.value,
        "entry": str(s.entry), "stop_loss": str(s.stop_loss), "target": str(s.target),
        "confidence": s.confidence, "reasoning": s.reasoning, "created_at": s.created_at.isoformat(),
        "timeframe": s.timeframe, "regime": s.regime,
        "max_hold_s": s.max_hold.total_seconds() if s.max_hold else None,
        "features": s.features, "id": s.id}, default=str)


def signal_from_json(raw: str) -> Signal:
    d = json.loads(raw)
    i = d["instrument"]
    return Signal(
        Instrument(Market(i["market"]), i["symbol"], i["exchange_token"], Decimal(i["lot_size"]),
                   Decimal(i["tick_size"])),
        Side(d["side"]), d["strategy"], Horizon(d["horizon"]), Decimal(d["entry"]),
        Decimal(d["stop_loss"]), Decimal(d["target"]), d["confidence"], d["reasoning"],
        datetime.fromisoformat(d["created_at"]), d["timeframe"], d["regime"],
        timedelta(seconds=d["max_hold_s"]) if d["max_hold_s"] is not None else None,
        d["features"], d["id"])


def _load(body: str) -> Proposal:
    d = json.loads(body)
    for k in ("created_at", "expires_at", "decided_at"):
        d[k] = datetime.fromisoformat(d[k]) if d[k] else None
    d["status"] = ProposalStatus(d["status"])
    return Proposal(**d)


class ProposalStore:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript(SCHEMA)

    def save(self, p: Proposal, signal: Signal | None = None) -> None:
        if signal is not None:
            self.db.execute(
                "INSERT INTO proposals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (p.id, p.status.value, p.symbol, p.strategy, p.side, p.created_at.isoformat(),
                 p.expires_at.isoformat(), _dump(p), signal_to_json(signal)))
        else:
            self.db.execute("UPDATE proposals SET status = ?, body = ? WHERE id = ?",
                            (p.status.value, _dump(p), p.id))

    def get(self, pid: str) -> tuple[Proposal, Signal] | None:
        row = self.db.execute("SELECT body, signal FROM proposals WHERE id = ?", (pid,)).fetchone()
        return (_load(row[0]), signal_from_json(row[1])) if row else None

    def pending(self) -> list[Proposal]:
        return [_load(r[0]) for r in self.db.execute(
            "SELECT body FROM proposals WHERE status = ? ORDER BY created_at", ("pending",))]

    def recent(self, limit: int = 20) -> list[Proposal]:
        return [_load(r[0]) for r in self.db.execute(
            "SELECT body FROM proposals ORDER BY created_at DESC LIMIT ?", (limit,))]

    def close(self) -> None:
        self.db.close()
