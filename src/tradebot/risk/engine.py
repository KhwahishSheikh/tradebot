"""Risk engine: the gate every order passes, twice (proposal stage and execution stage).

assess() is pure: (signal, portfolio state, market context) -> RiskDecision. It runs ALL checks
and reports every failure (not just the first) so the proposal/Telegram message can say exactly
why a trade was blocked. Halts that a check decides to raise are returned in `new_halts`;
RiskManager persists them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tradebot.config import AppConfig
from tradebot.core.interfaces import RiskStage
from tradebot.core.models import Side, Signal
from tradebot.risk.events import EventCalendar
from tradebot.risk.sizing import ContractSpec, Sizing, size_position
from tradebot.risk.state import HaltKind, PortfolioState, ist_midnight_after
from tradebot.timing.clock import TradingClock


@dataclass(frozen=True)
class MarketContext:
    now: datetime
    live_price: float | None = None        # required at EXECUTION
    last_bar_close: datetime | None = None
    bar_seconds: int = 3600


@dataclass
class RiskDecision:
    allowed: bool
    stage: RiskStage
    reasons: list[str] = field(default_factory=list)     # blocking
    warnings: list[str] = field(default_factory=list)    # non-blocking
    sizing: Sizing | None = None
    new_halts: list[tuple[HaltKind, str, datetime | None]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.allowed


class CryptoRiskEngine:
    def __init__(self, cfg: AppConfig, specs: dict[str, ContractSpec], clock: TradingClock,
                 events: EventCalendar):
        self.cfg, self.r = cfg, cfg.risk
        self.specs, self.clock, self.events = specs, clock, events

    def assess(self, sig: Signal, state: PortfolioState, stage: RiskStage,
               ctx: MarketContext) -> RiskDecision:
        d = RiskDecision(False, stage)
        sym, market = sig.instrument.symbol, sig.instrument.market
        self._account_limits(state, ctx, d)
        self._signal_sanity(sig, d)

        gate = (self.clock.can_propose if stage is RiskStage.PROPOSAL else self.clock.can_execute)(
            market, sig.horizon, ctx.now)
        d.reasons += [f"timing: {r}" for r in gate.reasons]
        d.warnings += list(gate.notes)

        if ev := self.events.active(market.value, ctx.now):
            d.reasons.append(f"event blackout: {ev.name} at {ev.at:%d %b %H:%M} UTC")
        if self.events.is_stale(ctx.now):
            d.warnings.append("event calendar has nothing in the next 30 days: update events.yaml")

        if ctx.last_bar_close is not None:
            age = (ctx.now - ctx.last_bar_close).total_seconds() / ctx.bar_seconds
            if age > self.r.max_data_age_bars:
                d.reasons.append(f"stale data: last bar closed {age:.1f} bars ago")

        entry = float(sig.entry)
        if stage is RiskStage.EXECUTION:
            if ctx.live_price is None:
                d.reasons.append("no live price at execution")
            else:
                entry = self._drift_check(sig, ctx.live_price, d)

        spec = self.specs.get(sym)
        if spec is None:
            d.reasons.append(f"no contract spec for {sym}")
        else:
            self._size(sig, spec, entry, state, d)

        d.allowed = not d.reasons
        return d

    # ---------- checks ----------

    def _account_limits(self, s: PortfolioState, ctx: MarketContext, d: RiskDecision) -> None:
        for h in s.halts:
            until = f" until {h.until:%d %b %H:%M} UTC" if h.until else " (manual resume needed)"
            d.reasons.append(f"halted [{h.kind.value}]: {h.reason}{until}")
        if s.drawdown_pct >= self.r.max_drawdown_pct:
            msg = f"drawdown {s.drawdown_pct:.1f}% >= {self.r.max_drawdown_pct}% limit"
            d.reasons.append(msg)
            d.new_halts.append((HaltKind.DRAWDOWN, msg, None))
        if -s.day_pnl_pct >= self.r.max_daily_loss_pct:
            msg = f"daily loss {s.day_pnl_pct:.2f}% hit {self.r.max_daily_loss_pct}% limit"
            d.reasons.append(msg)
            d.new_halts.append((HaltKind.DAILY_LOSS, msg, ist_midnight_after(ctx.now)))
        n, last = s.consecutive_losses()
        if n >= self.r.max_consecutive_losses and last is not None:
            until = last + timedelta(hours=self.r.loss_cooldown_hours)
            if ctx.now < until:
                msg = f"{n} losses in a row: cooling down"
                d.reasons.append(f"{msg} until {until:%d %b %H:%M} UTC")
                d.new_halts.append((HaltKind.COOLDOWN, msg, until))

    def _signal_sanity(self, sig: Signal, d: RiskDecision) -> None:
        e, sl, tp = float(sig.entry), float(sig.stop_loss), float(sig.target)
        if not sl:
            d.reasons.append("missing stop-loss")
        elif sig.side is Side.BUY and not (sl < e < tp):
            d.reasons.append(f"BUY needs stop < entry < target (got {sl}/{e}/{tp})")
        elif sig.side is Side.SELL and not (tp < e < sl):
            d.reasons.append(f"SELL needs target < entry < stop (got {tp}/{e}/{sl})")

    def _drift_check(self, sig: Signal, live: float, d: RiskDecision) -> float:
        e, sl = float(sig.entry), float(sig.stop_loss)
        drift = (live - e) / e * 100
        if abs(drift) > self.r.max_price_drift_pct:
            d.reasons.append(f"price drifted {drift:+.2f}% since signal (max "
                             f"{self.r.max_price_drift_pct}%): send a fresh proposal")
        if (sig.side is Side.BUY and live <= sl) or (sig.side is Side.SELL and live >= sl):
            d.reasons.append("live price already beyond the stop-loss")
        return live  # size off the real entry price, stop unchanged

    def _size(self, sig: Signal, spec: ContractSpec, entry: float, s: PortfolioState,
              d: RiskDecision) -> None:
        r, usd_inr = self.r, self.cfg.crypto.usd_inr
        equity_usd = s.equity_inr / usd_inr
        open_ = s.open_positions
        if len(open_) >= r.max_open_positions:
            d.reasons.append(f"{len(open_)} positions open (max {r.max_open_positions})")
        same = [p for p in open_ if p.symbol == sig.instrument.symbol]
        if len(same) >= r.max_positions_per_symbol:
            d.reasons.append(f"already {len(same)} position(s) in {sig.instrument.symbol}")
        heat_left = equity_usd * r.max_portfolio_heat_pct / 100 - sum(p.risk_usd for p in open_)
        notional_left = equity_usd * r.max_gross_leverage - sum(p.notional_usd for p in open_)
        if heat_left <= 0:
            d.reasons.append(f"portfolio heat at {r.max_portfolio_heat_pct}% cap")
        if notional_left <= 0:
            d.reasons.append(f"gross leverage at {r.max_gross_leverage}x cap")

        res = size_position(spec, sig.side, entry, float(sig.stop_loss),
                            equity_inr=s.equity_inr, usd_inr=usd_inr,
                            risk_pct=r.max_risk_per_trade_pct,
                            order_leverage=self.cfg.crypto.max_leverage,
                            max_notional_usd=max(0.0, notional_left),
                            max_risk_usd=max(0.0, heat_left),
                            round_trip_cost_pct=self.cfg.costs.crypto.round_trip_pct)
        if isinstance(res, str):
            d.reasons.append(f"sizing: {res}")
            return
        d.sizing = res
        if res.limited_by != "risk per trade":
            d.warnings.append(f"size reduced by {res.limited_by}")
        liq_dist, stop_dist = abs(entry - res.liq_price), abs(entry - float(sig.stop_loss))
        if liq_dist < r.min_liq_buffer * stop_dist:
            d.reasons.append(f"liquidation {res.liq_price:,.1f} too close: only "
                             f"{liq_dist / stop_dist:.1f}x the stop distance (min {r.min_liq_buffer}x)")
        if res.risk_pct > r.max_risk_per_trade_pct + 1e-9:
            d.reasons.append(f"risk {res.risk_pct:.2f}% > {r.max_risk_per_trade_pct}%")
