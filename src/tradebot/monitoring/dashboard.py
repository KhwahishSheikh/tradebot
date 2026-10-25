"""Read-only Streamlit dashboard. Run: `tradebot dashboard` (binds 127.0.0.1; on the VPS reach it
through an SSH tunnel: ssh -L 8501:localhost:8501 vps). Reads only the SQLite stores + heartbeat,
never the market-data DuckDB, so it can't interfere with the running bot."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from tradebot.config import load_config
from tradebot.execution.orders import ExecutionStore, PositionState
from tradebot.monitoring.health import read_heartbeat
from tradebot.proposals.store import ProposalStore
from tradebot.risk.state import RiskStateStore
from tradebot.timeutil import IST, now_utc

st.set_page_config(page_title="tradebot", layout="wide")
cfg, _ = load_config("config", ".env")
d = cfg.data.db_path.parent

hb = read_heartbeat(cfg.monitoring.heartbeat_file)
age = (now_utc() - datetime.fromisoformat(hb["ts"])).total_seconds() if hb else None
alive = age is not None and age < cfg.monitoring.watchdog_stale_seconds
st.title(f"tradebot · {cfg.mode.value.upper()}")
st.caption(("🟢 running" if alive else "🔴 NOT RUNNING") +
           (f" · heartbeat {age:.0f}s ago" if age is not None else " · no heartbeat yet") +
           f" · strategies: {', '.join(e.name for e in cfg.strategies.crypto if e.enabled)}")

risk = RiskStateStore(d / "risk_state.db", cfg.account.capital_inr)
s = risk.state()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Equity", f"₹{s.equity_inr:,.0f}", f"{(s.equity_inr / cfg.account.capital_inr - 1) * 100:+.2f}% total")
c2.metric("Today", f"{s.day_pnl_pct:+.2f}%", f"limit -{cfg.risk.max_daily_loss_pct}%", delta_color="off")
c3.metric("Drawdown", f"{s.drawdown_pct:.2f}%", f"limit {cfg.risk.max_drawdown_pct}%", delta_color="off")
c4.metric("Loss streak", s.consecutive_losses()[0], f"cooldown at {cfg.risk.max_consecutive_losses}",
          delta_color="off")
if s.halts:
    st.error("HALTED: " + " · ".join(f"{h.kind.value}: {h.reason}" for h in s.halts))

marks = pd.DataFrame(risk.db.execute("SELECT ts, equity_inr FROM equity_marks ORDER BY ts").fetchall(),
                     columns=["ts", "equity_inr"])
if not marks.empty:
    marks["ts"] = pd.to_datetime(marks["ts"], utc=True).dt.tz_convert(IST)
    st.subheader("Equity")
    st.line_chart(marks.set_index("ts")["equity_inr"])

ex = ExecutionStore(cfg.execution.db_path)
st.subheader("Open positions")
open_ = ex.positions()
st.dataframe(pd.DataFrame([{"id": p.proposal_id, "symbol": p.symbol, "side": p.side, "contracts": p.contracts,
                            "entry": p.entry_price, "stop": p.stop, "target": p.target,
                            "exchange stop": "OK" if p.bracket_ok else "MISSING", "exit by": p.exit_by[:16]}
                           for p in open_]) if open_ else pd.DataFrame(), width="stretch")

ps = ProposalStore(d / "proposals.db")
st.subheader("Pending approvals")
pend = ps.pending()
st.dataframe(pd.DataFrame([{"id": p.id, "symbol": p.symbol, "side": p.side, "contracts": p.contracts,
                            "entry": p.entry, "risk ₹": round(p.risk_inr), "conf": p.confidence,
                            "expires (IST)": p.expires_at.astimezone(IST).strftime("%H:%M:%S")}
                           for p in pend]) if pend else pd.DataFrame(), width="stretch")

st.subheader("Closed trades by strategy")
closed = ex.positions((PositionState.CLOSED,))
if closed:
    df = pd.DataFrame([{"strategy": p.strategy, "symbol": p.symbol, "reason": p.exit_reason,
                        "pnl_inr": (p.pnl_usd or 0) * cfg.crypto.usd_inr,
                        "r": ((p.pnl_usd or 0) * cfg.crypto.usd_inr / p.risk_inr) if p.risk_inr else 0}
                       for p in closed])
    g = df.groupby("strategy").agg(trades=("pnl_inr", "size"), win_pct=("pnl_inr", lambda x: 100 * (x > 0).mean()),
                                   avg_R=("r", "mean"), pnl_inr=("pnl_inr", "sum")).round(2)
    st.dataframe(g, width="stretch")
else:
    st.caption("No closed trades yet.")

st.subheader("Decisions (last 30 proposals)")
recent = ps.recent(30)
st.dataframe(pd.DataFrame([{"created (IST)": p.created_at.astimezone(IST).strftime("%d %b %H:%M"),
                            "id": p.id, "symbol": p.symbol, "side": p.side, "status": p.status.value,
                            "by": p.decided_by, "note": p.note[:80]} for p in recent])
             if recent else pd.DataFrame(), width="stretch")

audit_dir = Path(cfg.logging.dir) / "audit"
files = sorted(audit_dir.glob("audit-*.jsonl"))[-1:] if audit_dir.exists() else []
if files:
    st.subheader("Today's audit log (latest 50)")
    rows = [json.loads(line) for line in files[0].read_text().splitlines()[-50:]]
    st.dataframe(pd.DataFrame([{"time": r["ts_ist"][11:19], "event": r["event"],
                                "data": json.dumps(r["data"])[:160]} for r in reversed(rows)]),
                 width="stretch")
