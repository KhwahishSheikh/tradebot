"""Managed orders + positions, their state machines, and persistence (SQLite data/execution.db)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from tradebot.timeutil import now_utc


class OrderKind(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"          # time exit, manual close, kill switch: reduce-only market


class OrderState(StrEnum):
    NEW = "new"
    OPEN = "open"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"      # IOC remainder lands here too
    REJECTED = "rejected"

    @property
    def done(self) -> bool:
        return self in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED)


class PositionState(StrEnum):
    OPEN = "open"           # filled + exchange-side bracket confirmed
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass
class OrderUpdate:
    """What the exchange says about an order."""
    client_id: str
    exchange_id: str | None
    state: OrderState
    filled: int
    avg_price: float | None
    error: str | None = None


@dataclass
class ManagedOrder:
    proposal_id: str
    symbol: str
    product_id: int
    kind: OrderKind
    side: str
    size: int
    order_type: str                    # limit | market
    limit_price: float | None = None
    reduce_only: bool = False
    time_in_force: str = "ioc"
    client_id: str = field(default_factory=lambda: "tb" + uuid4().hex[:24])   # <= 32 chars
    exchange_id: str | None = None
    state: OrderState = OrderState.NEW
    filled: int = 0
    avg_price: float | None = None
    error: str | None = None
    attempts: int = 0
    created_at: str = field(default_factory=lambda: now_utc().isoformat())

    def apply(self, u: OrderUpdate) -> None:
        self.exchange_id = u.exchange_id or self.exchange_id
        self.state, self.filled = u.state, u.filled
        self.avg_price = u.avg_price if u.avg_price is not None else self.avg_price
        self.error = u.error or self.error


@dataclass
class ManagedPosition:
    proposal_id: str
    symbol: str
    product_id: int
    side: str                          # buy = long
    contracts: int
    contract_value: float
    entry_price: float
    stop: float
    target: float
    opened_at: str
    exit_by: str                       # max-hold deadline (Ticket 10 enforces)
    strategy: str = ""
    risk_inr: float = 0.0
    horizon: str = "swing"
    initial_stop: float = 0.0          # for R multiples (stop may trail later)
    trail_dist: float = 0.0            # price distance for the trailing stop (0 = off)
    extensions: int = 0
    target_prompted: bool = False
    state: PositionState = PositionState.OPEN
    bracket_ok: bool = False
    exit_price: float | None = None
    exit_reason: str | None = None
    closed_at: str | None = None
    pnl_usd: float | None = None

    @property
    def signed(self) -> int:
        return self.contracts if self.side == "buy" else -self.contracts

    def r_multiple(self, price: float) -> float:
        risk = abs(self.entry_price - (self.initial_stop or self.stop))
        sgn = 1 if self.side == "buy" else -1
        return sgn * (price - self.entry_price) / risk if risk else 0.0


@dataclass
class ExitRequest:
    """A question to you about an OPEN position. No answer -> default_action (always the safe one)."""
    proposal_id: str
    kind: str                      # extend_hold | target_reached
    created_at: str
    expires_at: str
    detail: str
    default_action: str = "close"
    status: str = "pending"        # pending | answered | timed_out
    answer: str | None = None
    by: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex[:8])


class ExecutionStore:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS orders (client_id TEXT PRIMARY KEY, proposal_id TEXT,
                state TEXT, body TEXT NOT NULL, updated TEXT);
            CREATE TABLE IF NOT EXISTS positions (proposal_id TEXT PRIMARY KEY, symbol TEXT,
                state TEXT, body TEXT NOT NULL, updated TEXT);
            CREATE TABLE IF NOT EXISTS exit_requests (id TEXT PRIMARY KEY, proposal_id TEXT,
                status TEXT, body TEXT NOT NULL);""")

    def save_order(self, o: ManagedOrder) -> None:
        self.db.execute("INSERT OR REPLACE INTO orders VALUES (?, ?, ?, ?, ?)",
                        (o.client_id, o.proposal_id, o.state.value, json.dumps(asdict(o)),
                         now_utc().isoformat()))

    def save_position(self, p: ManagedPosition) -> None:
        self.db.execute("INSERT OR REPLACE INTO positions VALUES (?, ?, ?, ?, ?)",
                        (p.proposal_id, p.symbol, p.state.value, json.dumps(asdict(p)),
                         now_utc().isoformat()))

    def positions(self, states: tuple[PositionState, ...] = (PositionState.OPEN, PositionState.CLOSING)
                  ) -> list[ManagedPosition]:
        q = ",".join("?" * len(states))
        rows = self.db.execute(f"SELECT body FROM positions WHERE state IN ({q})",
                               [s.value for s in states]).fetchall()
        out = []
        for (body,) in rows:
            d = json.loads(body)
            d["state"] = PositionState(d["state"])
            out.append(ManagedPosition(**d))
        return out

    def position(self, proposal_id: str) -> ManagedPosition | None:
        row = self.db.execute("SELECT body FROM positions WHERE proposal_id = ?", (proposal_id,)).fetchone()
        if not row:
            return None
        d = json.loads(row[0])
        d["state"] = PositionState(d["state"])
        return ManagedPosition(**d)

    def orders(self, proposal_id: str) -> list[ManagedOrder]:
        out = []
        for (body,) in self.db.execute("SELECT body FROM orders WHERE proposal_id = ?", (proposal_id,)):
            d = json.loads(body)
            d["kind"], d["state"] = OrderKind(d["kind"]), OrderState(d["state"])
            out.append(ManagedOrder(**d))
        return out

    def save_request(self, r: ExitRequest) -> None:
        self.db.execute("INSERT OR REPLACE INTO exit_requests VALUES (?, ?, ?, ?)",
                        (r.id, r.proposal_id, r.status, json.dumps(asdict(r))))

    def requests(self, status: str | None = "pending") -> list[ExitRequest]:
        q = "SELECT body FROM exit_requests" + (" WHERE status = ?" if status else "")
        return [ExitRequest(**json.loads(b)) for (b,) in self.db.execute(q, (status,) if status else ())]

    def request(self, rid: str) -> ExitRequest | None:
        row = self.db.execute("SELECT body FROM exit_requests WHERE id = ?", (rid,)).fetchone()
        return ExitRequest(**json.loads(row[0])) if row else None

    def close(self) -> None:
        self.db.close()


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)
