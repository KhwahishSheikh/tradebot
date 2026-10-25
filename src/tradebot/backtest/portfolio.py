"""Portfolio simulation that runs every signal through the SAME CryptoRiskEngine the live bot uses
(proposal-stage and execution-stage checks, sizing, halts, cooldowns, heat and leverage caps).

Simplifications (documented in the report):
  - equity for risk checks is REALISED equity (open trades are not marked to market for the
    daily-loss / drawdown checks); the reported equity curve IS marked to market daily
  - no historical macro-event blackouts (events.yaml only lists upcoming events)
  - no news (no historical labelled news exists yet)
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tradebot.backtest.execution import ExecData, TradeSim, simulate_trade
from tradebot.config import AppConfig
from tradebot.core.interfaces import RiskStage
from tradebot.core.models import Signal
from tradebot.risk.engine import CryptoRiskEngine, MarketContext
from tradebot.risk.state import Halt, HaltKind, OpenRisk, PortfolioState
from tradebot.timeutil import IST


@dataclass
class SignalEvent:
    signal: Signal
    bar_close: datetime
    regime: str


@dataclass
class Trade:
    strategy: str
    symbol: str
    side: str
    regime: str
    signal_ts: datetime
    sim: TradeSim
    contracts: int
    qty: float                       # underlying units
    pnl_usd: float
    pnl_inr: float
    risk_inr: float
    equity_before_inr: float

    @property
    def r(self) -> float:
        return self.pnl_inr / self.risk_inr if self.risk_inr else 0.0


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    blocked: list[tuple[datetime, str, str, str]] = field(default_factory=list)  # ts, strat, stage, reason
    equity_points: list[tuple[datetime, float]] = field(default_factory=list)   # realised
    halts: list[Halt] = field(default_factory=list)
    start_equity: float = 0.0


class HaltBook:
    def __init__(self):
        self.halts: list[Halt] = []

    def add(self, kind: HaltKind, reason: str, until: datetime | None, at: datetime):
        if not any(h.kind is kind for h in self.active(at)):
            self.halts.append(Halt(len(self.halts) + 1, kind, reason, at, until))

    def active(self, at: datetime) -> list[Halt]:
        return [h for h in self.halts if h.created_at <= at and (h.until is None or h.until > at)]


def run_portfolio(cfg: AppConfig, engine: CryptoRiskEngine, events: list[SignalEvent],
                  exec_data: dict[str, ExecData], delay: timedelta) -> BacktestResult:
    usd_inr = cfg.crypto.usd_inr
    res = BacktestResult(start_equity=cfg.account.capital_inr)
    equity = peak = day_start = cfg.account.capital_inr
    day = None
    open_heap: list[tuple[datetime, int, Trade, OpenRisk]] = []
    results: list[tuple[datetime, float]] = []
    halts = HaltBook()
    seq = 0

    def roll_day(t: datetime):
        nonlocal day, day_start
        d = t.astimezone(IST).date()
        if d != day:
            day, day_start = d, equity

    def realise_until(t: datetime):
        nonlocal equity, peak
        while open_heap and open_heap[0][0] <= t:
            exit_ts, _, tr, _ = heapq.heappop(open_heap)
            roll_day(exit_ts)
            equity += tr.pnl_inr
            peak = max(peak, equity)
            results.append((exit_ts, tr.pnl_inr))
            res.equity_points.append((exit_ts, equity))

    def state(at: datetime) -> PortfolioState:
        return PortfolioState(equity, day_start, peak, [o for *_, o in open_heap],
                              results[-20:], halts.active(at))

    for ev in sorted(events, key=lambda e: e.bar_close):
        sig, t0 = ev.signal, ev.bar_close
        realise_until(t0)
        roll_day(t0)
        sym = sig.instrument.symbol
        ex = exec_data.get(sym)
        if ex is None:
            continue

        d1 = engine.assess(sig, state(t0), RiskStage.PROPOSAL, MarketContext(t0, None, t0))
        for k, why, until in d1.new_halts:
            halts.add(k, why, until, t0)
        if not d1:
            res.blocked.append((t0, sig.strategy, "proposal", d1.reasons[0]))
            continue

        x = cfg.exits
        trail = (x.trail_atr_mult * float(sig.features.get("atr_pct", 0)) * float(sig.entry)
                 if x.trailing_enabled else 0.0)
        sim = simulate_trade(ex, sig.side, t0, float(sig.entry), float(sig.stop_loss),
                             float(sig.target), sig.max_hold, cfg.costs.crypto, delay,
                             cfg.risk.max_price_drift_pct, trail, x.trail_activate_r)
        if sim.status != "filled":
            res.blocked.append((t0, sig.strategy, "execution", sim.reason))
            continue
        realise_until(sim.entry_ts)
        roll_day(sim.entry_ts)
        # size off the expected FILL (slippage included) so realised risk never exceeds the cap
        d2 = engine.assess(sig, state(sim.entry_ts), RiskStage.EXECUTION,
                           MarketContext(sim.entry_ts, sim.entry_px, t0))
        for k, why, until in d2.new_halts:
            halts.add(k, why, until, sim.entry_ts)
        if not d2:
            res.blocked.append((t0, sig.strategy, "execution", d2.reasons[0]))
            continue

        spec = engine.specs[sym]
        qty = d2.sizing.contracts * spec.contract_value
        pnl_usd = qty * sim.net_pu
        tr = Trade(sig.strategy, sym, sig.side.value, ev.regime, t0, sim, d2.sizing.contracts, qty,
                   pnl_usd, pnl_usd * usd_inr, qty * sim.risk_pu * usd_inr, equity)
        res.trades.append(tr)
        o = OpenRisk(sym, sig.side.value, d2.sizing.contracts, sim.entry_px, float(sig.stop_loss),
                     spec.contract_value)
        seq += 1
        heapq.heappush(open_heap, (sim.exit_ts, seq, tr, o))

    realise_until(datetime.max.replace(tzinfo=IST))
    res.halts = halts.halts
    return res
