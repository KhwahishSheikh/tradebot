"""RiskManager: wires the pure engine to persistent state + the audit trail. The only door to orders."""

from __future__ import annotations

import logging

from tradebot.audit import AuditEvent, AuditLog
from tradebot.core.interfaces import RiskStage
from tradebot.core.models import Signal
from tradebot.risk.engine import CryptoRiskEngine, MarketContext, RiskDecision
from tradebot.risk.state import HaltKind, OpenRisk, RiskStateStore

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, engine: CryptoRiskEngine, store: RiskStateStore, audit: AuditLog | None,
                 open_positions=None):
        self.engine, self.store, self.audit = engine, store, audit
        self.open_positions = open_positions      # callable -> list[OpenRisk] (execution engine)

    def check(self, sig: Signal, stage: RiskStage, ctx: MarketContext,
              open_positions: list[OpenRisk] | None = None) -> RiskDecision:
        if open_positions is None and self.open_positions is not None:
            open_positions = self.open_positions()
        state = self.store.state(open_positions, ctx.now)
        d = self.engine.assess(sig, state, stage, ctx)
        for kind, reason, until in d.new_halts:
            self.store.halt(kind, reason, until, ctx.now)
            log.warning("HALT %s: %s", kind.value, reason)
        if self.audit:
            self.audit.record(
                AuditEvent.RISK_CHECK_PASSED if d.allowed else AuditEvent.RISK_CHECK_FAILED,
                signal_id=sig.id, stage=stage.value, symbol=sig.instrument.symbol,
                side=sig.side.value, strategy=sig.strategy, reasons=d.reasons,
                warnings=d.warnings, sizing=d.sizing, equity_inr=state.equity_inr)
        return d

    def kill_switch(self, reason: str, by: str) -> None:
        """Blocks all new entries immediately. Order cancel + flatten hooks are added in Ticket 9."""
        self.store.halt(HaltKind.KILL_SWITCH, f"{reason} (by {by})")
        if self.audit:
            self.audit.record(AuditEvent.KILL_SWITCH, reason=reason, by=by)
        log.critical("KILL SWITCH by %s: %s", by, reason)

    def resume(self, by: str, kinds: set[HaltKind] | None = None) -> int:
        n = self.store.resume(by, kinds)
        if self.audit:
            self.audit.record(AuditEvent.MODE_CHANGE, action="resume", by=by,
                              kinds=sorted(k.value for k in (kinds or set(HaltKind))), cleared=n)
        return n
