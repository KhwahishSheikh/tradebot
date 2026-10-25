"""Heartbeat file, watchdog and the daily summary."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from tradebot.config import AppConfig
from tradebot.core.models import ProposalStatus
from tradebot.execution.orders import PositionState
from tradebot.timeutil import IST, now_utc


def write_heartbeat(path: Path, **info) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ts": now_utc().isoformat(), "pid": os.getpid(), **info}, default=str))
    tmp.replace(path)


def read_heartbeat(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def watchdog(cfg: AppConfig, send) -> tuple[bool, str]:
    """Run from cron/systemd every few minutes, OUTSIDE the bot process. Alerts once per outage."""
    hb = read_heartbeat(cfg.monitoring.heartbeat_file)
    marker = cfg.monitoring.heartbeat_file.with_suffix(".alerted")
    if hb is None:
        age = None
    else:
        age = (now_utc() - datetime.fromisoformat(hb["ts"])).total_seconds()
    healthy = age is not None and age <= cfg.monitoring.watchdog_stale_seconds
    if healthy:
        if marker.exists():
            marker.unlink()
            send("✅ tradebot heartbeat is back.")
        return True, f"ok (heartbeat {age:.0f}s ago)"
    msg = ("🚨 tradebot is DOWN: no heartbeat " + (f"for {age / 60:.0f} min" if age else "file found")
           + ". New entries are not being proposed. Exchange-side stops still protect open positions.")
    if not marker.exists():
        send(msg)
        marker.write_text(now_utc().isoformat())
    return False, msg


def daily_summary(cfg: AppConfig, proposals_store, exec_store, risk_store, day=None) -> str:
    today = day or now_utc().astimezone(IST).date()
    in_day = lambda iso: iso and datetime.fromisoformat(iso).astimezone(IST).date() == today
    props = [p for p in proposals_store.recent(500) if p.created_at.astimezone(IST).date() == today]
    counts = {s: sum(1 for p in props if p.status is s) for s in ProposalStatus}
    closed = [p for p in exec_store.positions((PositionState.CLOSED,)) if in_day(p.closed_at)]
    pnl = sum((p.pnl_usd or 0) for p in closed) * cfg.crypto.usd_inr
    st = risk_store.state()
    lines = [f"📊 Daily summary {today:%a %d %b %Y} ({cfg.mode.value})",
             (f"Proposals: {len(props)} sent | {counts[ProposalStatus.EXECUTED]} executed | "
             f"{counts[ProposalStatus.REJECTED]} rejected by you | {counts[ProposalStatus.EXPIRED]} expired | "
             f"{counts[ProposalStatus.RISK_BLOCKED]} blocked at execution | "
             f"{counts[ProposalStatus.DRIFT_CANCELLED]} drift-cancelled"),
             f"Closed trades: {len(closed)} | realised P&L ₹{pnl:+,.0f}"]
    lines += [f"  {p.symbol} {p.side} {p.exit_reason}: ₹{(p.pnl_usd or 0) * cfg.crypto.usd_inr:+,.0f}"
              for p in closed]
    lines.append(f"Equity ₹{st.equity_inr:,.0f} | today {st.day_pnl_pct:+.2f}% | "
                 f"drawdown {st.drawdown_pct:.2f}%")
    if st.halts:
        lines.append("Halts: " + ", ".join(f"{h.kind.value} ({h.reason})" for h in st.halts))
    return "\n".join(lines)


def next_daily(hhmm: str, after: datetime) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    local = after.astimezone(IST)
    t = local.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > local else t + timedelta(days=1)
