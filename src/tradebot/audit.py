"""Append-only audit trail (signals, proposals, decisions, orders, fills, P&L, errors).

One JSON object per line, one file per IST trading date: logs/audit/audit-YYYY-MM-DD.jsonl.
Files are never rotated away or rewritten. They are the source for the tax and review exports.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any

from tradebot.config import Mode
from tradebot.timeutil import IST, now_utc


class AuditEvent(StrEnum):
    SYSTEM_START = "system_start"
    SYSTEM_STOP = "system_stop"
    MODE_CHANGE = "mode_change"
    SIGNAL = "signal"
    PROPOSAL_CREATED = "proposal_created"
    PROPOSAL_SENT = "proposal_sent"
    PROPOSAL_APPROVED = "proposal_approved"
    PROPOSAL_REJECTED = "proposal_rejected"
    PROPOSAL_MODIFIED = "proposal_modified"
    PROPOSAL_EXPIRED = "proposal_expired"
    PROPOSAL_DRIFT_CANCELLED = "proposal_drift_cancelled"
    RISK_CHECK_PASSED = "risk_check_passed"
    RISK_CHECK_FAILED = "risk_check_failed"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_UPDATE = "order_update"
    ORDER_REJECTED = "order_rejected"
    FILL = "fill"
    POSITION_CLOSED = "position_closed"
    PNL_SNAPSHOT = "pnl_snapshot"
    EXIT_REQUEST = "exit_request"
    EXIT_DECISION = "exit_decision"
    POSITION_REVIEW = "position_review"
    KILL_SWITCH = "kill_switch"
    UNAUTHORIZED_SENDER = "unauthorized_sender"
    ERROR = "error"


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)  # keep exact precision for tax records
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Enum):
        return o.value
    if is_dataclass(o):
        return asdict(o)
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


class AuditLog:
    def __init__(self, log_dir: Path | str, mode: Mode):
        self.dir = Path(log_dir) / "audit"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self._lock = threading.Lock()

    def record(self, event: AuditEvent, **data: Any) -> dict[str, Any]:
        ts = now_utc()
        ts_ist = ts.astimezone(IST)
        entry = {
            "ts_utc": ts.isoformat(),
            "ts_ist": ts_ist.isoformat(),
            "mode": self.mode.value,
            "event": event.value,
            "data": data,
        }
        line = json.dumps(entry, default=_default, ensure_ascii=False)
        path = self.dir / f"audit-{ts_ist.date().isoformat()}.jsonl"
        with self._lock, open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())  # survive a crash right after an order
        return entry
