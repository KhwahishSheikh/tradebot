"""Trade proposal: everything you need to decide, frozen at creation, plus its decision history."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from tradebot.core.models import ProposalStatus, Signal
from tradebot.risk.sizing import Sizing
from tradebot.timeutil import IST

TERMINAL = {ProposalStatus.REJECTED, ProposalStatus.EXPIRED, ProposalStatus.DRIFT_CANCELLED,
            ProposalStatus.RISK_BLOCKED, ProposalStatus.EXECUTED}


@dataclass
class Proposal:
    id: str
    created_at: datetime
    expires_at: datetime
    status: ProposalStatus
    market: str
    symbol: str
    side: str
    strategy: str
    horizon: str
    timeframe: str
    regime: str
    entry: float
    stop: float
    target: float
    confidence: float
    reasoning: str
    contracts: int
    max_contracts: int              # what the risk engine allowed; MODIFY can only go below it
    notional_usd: float
    risk_inr: float
    risk_pct: float
    order_leverage: float
    effective_leverage: float
    liq_price: float
    max_hold_hours: float
    signal_id: str
    version: int = 1
    decided_at: datetime | None = None
    decided_by: str | None = None
    note: str = ""
    supersedes: str | None = None    # id of the proposal this one replaced (drift refresh)
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status is ProposalStatus.PENDING

    @property
    def rr(self) -> float:
        risk = abs(self.entry - self.stop)
        return abs(self.target - self.entry) / risk if risk else 0.0

    def to_signal(self, base: Signal) -> Signal:
        """The signal as currently proposed (after any MODIFY)."""
        from dataclasses import replace
        return replace(base, entry=Decimal(f"{self.entry:.2f}"), stop_loss=Decimal(f"{self.stop:.2f}"),
                       target=Decimal(f"{self.target:.2f}"))

    def apply_sizing(self, s: Sizing, contracts: int | None = None) -> None:
        self.max_contracts = s.contracts
        n = s.contracts if contracts is None else min(contracts, s.contracts)
        scale = n / s.contracts
        self.contracts = n
        self.notional_usd = s.notional_usd * scale
        self.risk_inr = s.risk_inr * scale
        self.risk_pct = s.risk_pct * scale
        self.order_leverage = s.order_leverage
        self.effective_leverage = s.effective_leverage * scale
        self.liq_price = s.liq_price

    def log(self, event: str, at: datetime, by: str | None = None, **data: Any) -> None:
        self.history.append({"event": event, "at": at.isoformat(), "by": by, **data})

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


def render(p: Proposal, now: datetime | None = None) -> str:
    """Plain-text card (Telegram message body in Ticket 11)."""
    arrow = "LONG" if p.side == "buy" else "SHORT"
    left = ""
    if now and p.is_open:
        secs = max(0, int((p.expires_at - now).total_seconds()))
        left = f" ({secs // 60}m {secs % 60}s left)"
    lines = [
        f"TRADE PROPOSAL #{p.id}  v{p.version}  [{p.status.value.upper()}]",
        f"{p.symbol} {arrow}  |  Delta crypto perpetual  |  {p.strategy} ({p.horizon}, {p.timeframe})",
        f"Entry {p.entry:,.2f}   SL {p.stop:,.2f}   Target {p.target:,.2f}   R:R {p.rr:.2f}",
        f"Size {p.contracts} contracts (max {p.max_contracts}), notional ${p.notional_usd:,.0f}",
        f"Capital at risk ₹{p.risk_inr:,.0f} ({p.risk_pct:.2f}% of account), all-in incl. fees",
        (f"Leverage: order {p.order_leverage:g}x, effective {p.effective_leverage:.2f}x, "
        f"est. liquidation {p.liq_price:,.1f}"),
        f"Confidence {p.confidence:.2f}  |  regime {p.regime}  |  max hold {p.max_hold_hours:.0f}h",
        f"Why: {p.reasoning}",
        f"Expires {p.expires_at.astimezone(IST):%d %b %H:%M:%S} IST{left}",
    ]
    if p.decided_by:
        lines.append(f"Decision: {p.status.value} by {p.decided_by} at "
                     f"{p.decided_at.astimezone(IST):%H:%M:%S} IST {('- ' + p.note) if p.note else ''}")
    return "\n".join(lines)
