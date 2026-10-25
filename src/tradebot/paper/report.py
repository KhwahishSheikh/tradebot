"""Paper-phase report + go-live readiness checklist (the gate for Ticket 13)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from tradebot.config import AppConfig
from tradebot.core.models import ProposalStatus
from tradebot.execution.orders import ExecutionStore, PositionState
from tradebot.paper.ledger import ForwardLedger
from tradebot.proposals.store import ProposalStore
from tradebot.risk.state import HaltKind, RiskStateStore
from tradebot.timeutil import IST, now_utc

DECIDED = {ProposalStatus.EXECUTED, ProposalStatus.REJECTED, ProposalStatus.RISK_BLOCKED,
           ProposalStatus.DRIFT_CANCELLED, ProposalStatus.APPROVED}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _strategy_table(fwd: pd.DataFrame) -> pd.DataFrame:
    if fwd.empty:
        return pd.DataFrame()
    ev = fwd[fwd["sim_status"] == "filled"]
    rows = []
    for (strat, enabled), g in fwd.groupby(["strategy", "enabled"]):
        e = ev[(ev["strategy"] == strat) & (ev["enabled"] == enabled)]
        rows.append({"strategy": strat, "mode": "LIVE-PAPER" if enabled else "shadow",
                     "signals": len(g), "evaluated": len(e),
                     "win_%": round(100 * (e["net_r"] > 0).mean(), 1) if len(e) else None,
                     "avg_R": round(e["net_r"].mean(), 3) if len(e) else None,
                     "sum_R": round(e["net_r"].sum(), 2) if len(e) else 0.0})
    return pd.DataFrame(rows)


def _audit_errors(cfg: AppConfig, since: datetime, needles: tuple[str, ...]) -> int:
    n = 0
    for f in sorted((Path(cfg.logging.dir) / "audit").glob("audit-*.jsonl")):
        for line in f.read_text().splitlines():
            r = json.loads(line)
            if r["event"] == "error" and r["ts_utc"] >= since.isoformat() and any(
                    s in json.dumps(r["data"]) for s in needles):
                n += 1
    return n


def build_report(cfg: AppConfig, ledger: ForwardLedger, proposals: ProposalStore,
                 execs: ExecutionStore, risk: RiskStateStore, backtest=None, now=None) -> tuple[str, list[Check]]:
    """backtest: optional callable (start, end) -> dict with 'trades' stats (paper vs backtest)."""
    now = now or now_utc()
    pc = cfg.paper
    start = ledger.started()
    weeks = (now - start).days / 7
    uptime = ledger.uptime_pct(start, now)
    fwd = ledger.frame()
    props = [p for p in proposals.recent(100_000) if p.created_at >= start]
    closed = [p for p in execs.positions((PositionState.CLOSED,))
              if datetime.fromisoformat(p.opened_at) >= start]

    # ---- your decisions ----
    by_status = pd.Series([p.status.value for p in props]).value_counts().to_dict() if props else {}
    decided = [p for p in props if p.status in DECIDED and p.decided_by not in (None, "system", "auto")]
    expired = [p for p in props if p.status is ProposalStatus.EXPIRED]
    lat = [(p.decided_at - p.created_at).total_seconds() / 60 for p in decided if p.decided_at]
    decided_pct = 100 * len(decided) / max(1, len(decided) + len(expired))

    outcome = {} if fwd.empty else dict(zip(fwd["proposal_id"], fwd["net_r"], strict=False))
    rej_rows = []
    for p in props:
        if p.status in (ProposalStatus.REJECTED, ProposalStatus.EXPIRED) and outcome.get(p.id) is not None:
            r = float(outcome[p.id])
            rej_rows.append((p, r, r * p.risk_inr))
    rej_inr = sum(v for *_, v in rej_rows)
    rej_line = ("no rejected/expired proposals with outcomes yet" if not rej_rows else
                f"{len(rej_rows)} rejected/expired proposals would have made ₹{rej_inr:+,.0f} "
                f"({'your rejections COST you money' if rej_inr > 0 else 'your rejections SAVED you money'})")

    # ---- execution quality ----
    slips = []
    for pos in closed:
        p = next((q for q in props if q.id == pos.proposal_id), None)
        if p:
            sgn = 1 if pos.side == "buy" else -1
            slips.append(sgn * (pos.entry_price - p.entry) / p.entry * 100)
    paper_r = [((pos.pnl_usd or 0) * cfg.crypto.usd_inr / pos.risk_inr) for pos in closed if pos.risk_inr]

    # ---- paper vs backtest, same window ----
    bt = backtest(start, now) if backtest else None
    bt_trades = bt["trades"].get("trades", 0) if bt else None
    bt_avg_r = bt["trades"].get("avg_R") if bt else None

    # ---- checklist ----
    en = fwd[(fwd["enabled"] == 1) & (fwd["sim_status"] == "filled")] if not fwd.empty else fwd
    n_fwd = len(en)
    fwd_r = float(en["net_r"].mean()) if n_fwd else float("nan")
    unprotected = _audit_errors(cfg, start, ("could NOT place exchange stop", "bracket missing"))
    reconcile_halts = [h for h in risk.active_halts() if h.kind is HaltKind.RECONCILE]
    checks = [
        Check("duration", weeks >= pc.min_weeks, f"{weeks:.1f} / {pc.min_weeks} weeks (target {pc.target_weeks})"),
        Check("uptime", uptime >= pc.uptime_target_pct, f"{uptime:.1f}% (need {pc.uptime_target_pct}%)"),
        Check("exchange stop always present", unprotected == 0, f"{unprotected} incidents"),
        Check("no unresolved reconcile halts", not reconcile_halts, f"{len(reconcile_halts)} active"),
        Check("you answer proposals in time", decided_pct >= pc.min_decided_pct or not props,
              f"{decided_pct:.0f}% decided before expiry ({len(expired)} expired)"),
        Check("forward sample size", n_fwd >= pc.min_forward_trades,
              f"{n_fwd} / {pc.min_forward_trades} evaluated forward trades (enabled strategies)"),
        Check("forward edge positive", n_fwd >= pc.min_forward_trades and fwd_r > 0,
              f"avg {fwd_r:+.3f}R" if n_fwd else "no data"),
    ]
    if bt is not None:
        n_paper = len(closed)
        count_ok = abs(n_paper - bt_trades) <= max(2, 0.25 * max(bt_trades, 1))
        r_ok = (not paper_r or bt_avg_r is None or
                abs(sum(paper_r) / len(paper_r) - bt_avg_r) <= pc.max_avg_r_gap)
        checks.append(Check("paper matches backtest", count_ok and r_ok,
                            f"paper {n_paper} trades vs backtest {bt_trades}; avg R paper "
                            f"{(sum(paper_r) / len(paper_r)) if paper_r else float('nan'):+.2f} "
                            f"vs backtest {bt_avg_r if bt_avg_r is not None else float('nan'):+.2f}"))
    if slips:
        avg_slip = sum(slips) / len(slips)
        checks.append(Check("entry slippage within model", avg_slip <= 2 * (cfg.costs.crypto.slippage_pct +
                                                                              cfg.execution.max_entry_slippage_pct / 2),
                            f"avg {avg_slip:+.3f}% vs signal price"))
    ready = all(c.ok for c in checks)

    # ---- text ----
    st = risk.state()
    lines = [f"# Paper-trading report ({now.astimezone(IST):%d %b %Y %H:%M} IST)", "",
             (f"Started {start.astimezone(IST):%d %b %Y}; {weeks:.1f} weeks; uptime {uptime:.1f}%; "
             f"equity ₹{st.equity_inr:,.0f} (DD {st.drawdown_pct:.2f}%)"), "",
             "## Your decisions", f"- proposals: {len(props)} {by_status}",
             f"- median decision time: {pd.Series(lat).median():.1f} min" if lat else "- no decisions yet",
             f"- {rej_line}"]
    lines += [f"  - #{p.id} {p.symbol} {p.side} ({p.status.value}): {r:+.2f}R = ₹{v:+,.0f}" for p, r, v in rej_rows[:10]]
    lines += ["", "## Paper trades", f"- closed: {len(closed)}; realised ₹"
              f"{sum((p.pnl_usd or 0) for p in closed) * cfg.crypto.usd_inr:+,.0f}"
              + (f"; avg {sum(paper_r) / len(paper_r):+.2f}R" if paper_r else "")
              + (f"; avg entry slippage {sum(slips) / len(slips):+.3f}%" if slips else "")]
    if bt is not None:
        lines.append(f"- backtest on the same window: {bt['trades']}")
    tbl = _strategy_table(fwd)
    lines += ["", "## Forward record by strategy (live data, out-of-sample)",
              tbl.to_markdown(index=False) if not tbl.empty else "no signals yet"]
    lines += ["", "## Go-live readiness: " + ("READY ✅" if ready else "NOT READY ❌")]
    lines += [f"- [{'x' if c.ok else ' '}] {c.name}: {c.detail}" for c in checks]
    if not ready:
        lines.append("\nLive trading stays off until every box is ticked.")
    return "\n".join(lines), checks


def short(text: str) -> str:
    """Telegram-sized version: decisions + readiness block."""
    keep, out = False, []
    for line in text.splitlines():
        if line.startswith(("## Your decisions", "## Go-live")):
            keep = True
        elif line.startswith("## "):
            keep = False
        if keep or line.startswith("Started"):
            out.append(line.replace("## ", ""))
    return "\n".join(out)


def since(days: int) -> datetime:
    return now_utc() - timedelta(days=days)
