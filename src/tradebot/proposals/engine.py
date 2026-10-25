"""Proposal & approval engine. The bot NEVER opens a position without passing through approve().

Lifecycle:  create -> PENDING -> (modify)* -> approve | reject | expire
  approve:  whitelist -> still pending -> not expired -> price drift (else cancel + fresh proposal)
            -> risk engine EXECUTION check (blocks even if you approved) -> executor -> EXECUTED
Every step is written to the audit log with timestamps and who did it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import uuid4

from tradebot.audit import AuditEvent, AuditLog
from tradebot.config import AppConfig
from tradebot.core.interfaces import RiskStage
from tradebot.core.models import Horizon, ProposalStatus, Side, Signal
from tradebot.proposals.model import Proposal, render
from tradebot.proposals.store import ProposalStore
from tradebot.risk.engine import MarketContext
from tradebot.risk.manager import RiskManager
from tradebot.risk.state import ist_midnight_after
from tradebot.timeutil import now_utc

log = logging.getLogger(__name__)

SYSTEM_USERS = {"cli", "auto"}   # cli = someone with shell access on the bot host


class Executor(Protocol):
    def execute(self, p: Proposal, signal: Signal, live_price: float) -> str: ...


class Notifier(Protocol):
    def proposal(self, p: Proposal, text: str) -> None: ...
    def message(self, text: str) -> None: ...


class PaperLogExecutor:
    """Placeholder until Ticket 9: records the order it WOULD place."""

    def __init__(self, audit: AuditLog | None):
        self.audit = audit

    def execute(self, p: Proposal, signal: Signal, live_price: float) -> str:
        ref = f"paper-{uuid4().hex[:10]}"
        if self.audit:
            self.audit.record(AuditEvent.ORDER_SUBMITTED, proposal_id=p.id, order_ref=ref,
                              symbol=p.symbol, side=p.side, contracts=p.contracts,
                              price_ref=live_price, stop=p.stop, target=p.target,
                              leverage=p.order_leverage, paper=True)
        return ref


class ConsoleNotifier:
    def proposal(self, p: Proposal, text: str) -> None:
        print(text)

    def message(self, text: str) -> None:
        print(text)


@dataclass
class Outcome:
    ok: bool
    message: str
    proposal: Proposal | None = None
    fresh: Proposal | None = None          # replacement after a drift cancel
    reasons: list[str] = field(default_factory=list)


class ProposalEngine:
    def __init__(self, cfg: AppConfig, risk: RiskManager, store: ProposalStore,
                 price: Callable[[str], float], executor: Executor, audit: AuditLog | None,
                 notifier: Notifier | None = None, clock: Callable[[], datetime] = now_utc):
        self.cfg, self.risk, self.store = cfg, risk, store
        self.price, self.executor, self.audit = price, executor, audit
        self.notifier, self.now = notifier, clock

    # ---------- helpers ----------

    def authorized(self, user: str) -> bool:
        if user in SYSTEM_USERS:
            return True
        return user.isdigit() and int(user) in self.cfg.telegram.allowed_user_ids

    def expiry_for(self, horizon: Horizon, at: datetime) -> datetime:
        e = self.cfg.approval.expiry_minutes
        if horizon is Horizon.INTRADAY:
            return at + timedelta(minutes=e.intraday)
        if horizon is Horizon.SWING:
            return at + timedelta(minutes=e.swing)
        return ist_midnight_after(at)                       # long-term: end of the IST day

    def _audit(self, event: AuditEvent, p: Proposal | None = None, **data) -> None:
        if self.audit:
            self.audit.record(event, **({"proposal_id": p.id, "version": p.version,
                                         "status": p.status.value} if p else {}), **data)

    def _load_open(self, pid: str, user: str, action: str) -> tuple[Proposal, Signal] | Outcome:
        if not self.authorized(user):
            self._audit(AuditEvent.UNAUTHORIZED_SENDER, user=user, action=action, target=pid)
            log.warning("unauthorized %s attempt by %s on %s", action, user, pid)
            return Outcome(False, "not authorized")
        got = self.store.get(pid)
        if not got:
            return Outcome(False, f"no proposal {pid}")
        p, sig = got
        if not p.is_open:
            return Outcome(False, f"proposal {pid} is already {p.status.value}", p)
        if self.now() >= p.expires_at:
            self._expire(p, "expired before " + action)
            return Outcome(False, f"proposal {pid} expired at {p.expires_at:%H:%M:%S} UTC; not executed", p)
        return p, sig

    def _finish(self, p: Proposal, status: ProposalStatus, by: str, note: str = "") -> None:
        p.status, p.decided_at, p.decided_by, p.note = status, self.now(), by, note
        p.log(status.value, p.decided_at, by, note=note)
        self.store.save(p)

    def _expire(self, p: Proposal, why: str) -> None:
        self._finish(p, ProposalStatus.EXPIRED, "system", why)
        self._audit(AuditEvent.PROPOSAL_EXPIRED, p, reason=why)
        if self.notifier:
            self.notifier.message(f"Proposal #{p.id} {p.symbol} expired: auto-rejected, nothing executed.")

    # ---------- create ----------

    def create(self, sig: Signal, ctx: MarketContext, supersedes: str | None = None) -> Outcome:
        self.expire_due()
        dup = [q for q in self.store.pending() if (q.symbol, q.strategy, q.side) ==
               (sig.instrument.symbol, sig.strategy, sig.side.value)]
        if dup:
            return Outcome(False, f"duplicate of pending #{dup[0].id}", dup[0])
        d = self.risk.check(sig, RiskStage.PROPOSAL, ctx)
        if not d.allowed:
            return Outcome(False, "blocked by risk engine", reasons=d.reasons)
        now = self.now()
        p = Proposal(
            id=uuid4().hex[:8], created_at=now, expires_at=self.expiry_for(sig.horizon, now),
            status=ProposalStatus.PENDING, market=sig.instrument.market.value,
            symbol=sig.instrument.symbol, side=sig.side.value, strategy=sig.strategy,
            horizon=sig.horizon.value, timeframe=sig.timeframe, regime=sig.regime,
            entry=float(sig.entry), stop=float(sig.stop_loss), target=float(sig.target),
            confidence=sig.confidence, reasoning=sig.reasoning, contracts=0, max_contracts=0,
            notional_usd=0, risk_inr=0, risk_pct=0, order_leverage=0, effective_leverage=0,
            liq_price=0, max_hold_hours=(sig.max_hold.total_seconds() / 3600) if sig.max_hold else 0,
            signal_id=sig.id, supersedes=supersedes)
        p.apply_sizing(d.sizing)
        p.log("created", now, "system", warnings=d.warnings)
        self.store.save(p, sig)
        self._audit(AuditEvent.PROPOSAL_CREATED, p, proposal=p.as_dict(), warnings=d.warnings)
        if self.notifier:
            self.notifier.proposal(p, render(p, now))
            self._audit(AuditEvent.PROPOSAL_SENT, p)
        aa = self.cfg.approval.auto_approve
        if aa.enabled and not self.cfg.is_live and p.risk_inr <= aa.max_risk_inr:
            return self.approve(p.id, "auto")
        return Outcome(True, f"proposal #{p.id} created", p)

    # ---------- decisions ----------

    def reject(self, pid: str, user: str, note: str = "") -> Outcome:
        got = self._load_open(pid, user, "reject")
        if isinstance(got, Outcome):
            return got
        p, _ = got
        self._finish(p, ProposalStatus.REJECTED, user, note)
        self._audit(AuditEvent.PROPOSAL_REJECTED, p, by=user, note=note)
        return Outcome(True, f"#{pid} rejected", p)

    def modify(self, pid: str, user: str, contracts: int | None = None,
               stop: float | None = None, target: float | None = None) -> Outcome:
        got = self._load_open(pid, user, "modify")
        if isinstance(got, Outcome):
            return got
        p, sig = got
        if contracts is not None and contracts < 1:
            return Outcome(False, "size must be at least 1 contract", p)
        before = {"contracts": p.contracts, "stop": p.stop, "target": p.target}
        cand = replace(p, stop=stop if stop is not None else p.stop,
                       target=target if target is not None else p.target, history=list(p.history))
        msig = cand.to_signal(sig)
        d = self.risk.check(msig, RiskStage.PROPOSAL, MarketContext(self.now()))
        if not d.allowed:
            return Outcome(False, "modification rejected by risk engine", p, reasons=d.reasons)
        cand.apply_sizing(d.sizing, contracts if contracts is not None else p.contracts)
        cand.version += 1
        after = {"contracts": cand.contracts, "stop": cand.stop, "target": cand.target}
        cand.log("modified", self.now(), user, before=before, after=after)
        self.store.save(cand)
        self._audit(AuditEvent.PROPOSAL_MODIFIED, cand, by=user, before=before, after=after)
        msg = f"#{pid} modified to v{cand.version}; still needs APPROVE"
        if contracts is not None and contracts > cand.contracts:
            msg += f" (size capped at {cand.contracts} by risk limits)"
        if self.notifier:
            self.notifier.proposal(cand, render(cand, self.now()))
        return Outcome(True, msg, cand)

    def approve(self, pid: str, user: str) -> Outcome:
        got = self._load_open(pid, user, "approve")
        if isinstance(got, Outcome):
            return got
        p, sig = got
        now = self.now()
        live = float(self.price(p.symbol))
        drift = (live - p.entry) / p.entry * 100
        if abs(drift) > self.cfg.approval.max_price_drift_pct:
            self._finish(p, ProposalStatus.DRIFT_CANCELLED, user,
                         f"price moved {drift:+.2f}% (max {self.cfg.approval.max_price_drift_pct}%)")
            self._audit(AuditEvent.PROPOSAL_DRIFT_CANCELLED, p, by=user, live=live, drift_pct=drift)
            fresh = self._refresh(p, sig, live)
            msg = f"#{pid} cancelled: price moved {drift:+.2f}% since proposal."
            msg += f" Fresh proposal #{fresh.id} sent." if fresh else " Setup no longer valid at this price."
            return Outcome(False, msg, p, fresh)

        msig = p.to_signal(sig)
        d = self.risk.check(msig, RiskStage.EXECUTION, MarketContext(now, live, sig.created_at))
        if not d.allowed:
            self._finish(p, ProposalStatus.RISK_BLOCKED, user, "; ".join(d.reasons))
            return Outcome(False, f"#{pid} approved but BLOCKED by risk engine", p, reasons=d.reasons)

        p.apply_sizing(d.sizing, p.contracts)          # never above what risk allows NOW
        p.entry = live
        p.status, p.decided_at, p.decided_by = ProposalStatus.APPROVED, now, user
        p.log("approved", now, user, live=live)
        self.store.save(p)
        self._audit(AuditEvent.PROPOSAL_APPROVED, p, by=user, live=live, contracts=p.contracts,
                    risk_inr=p.risk_inr)
        try:
            ref = self.executor.execute(p, msig, live)
        except Exception as e:  # noqa: BLE001 - surface, never retry blindly here
            p.note = f"execution error: {e}"
            p.log("execution_error", self.now(), "system", error=str(e))
            self.store.save(p)
            self._audit(AuditEvent.ERROR, p, error=str(e), where="executor")
            return Outcome(False, f"#{pid} approved but execution failed: {e}", p)
        self._finish(p, ProposalStatus.EXECUTED, user, f"order {ref}")
        return Outcome(True, f"#{pid} executed: {p.contracts} contracts {p.side} {p.symbol} "
                             f"@ ~{live:,.2f} (order {ref})", p)

    def _refresh(self, old: Proposal, sig: Signal, live: float) -> Proposal | None:
        """Same idea at today's price: keep stop + target, re-check R:R and risk from scratch."""
        long_ = old.side == Side.BUY.value
        if (long_ and not old.stop < live < old.target) or (not long_ and not old.target < live < old.stop):
            return None
        rr = abs(old.target - live) / abs(live - old.stop)
        if rr < self.cfg.strategies.min_net_rr:
            return None
        new_sig = replace(sig, entry=Decimal(f"{live:.2f}"), id=uuid4().hex[:12],
                          reasoning=sig.reasoning + f" [refreshed after drift from #{old.id}]")
        out = self.create(new_sig, MarketContext(self.now(), None, sig.created_at), supersedes=old.id)
        return out.proposal if out.ok else None

    # ---------- housekeeping ----------

    def expire_due(self) -> list[Proposal]:
        now, out = self.now(), []
        for p in self.store.pending():
            if now >= p.expires_at:
                self._expire(p, "not approved in time")
                out.append(p)
        return out
