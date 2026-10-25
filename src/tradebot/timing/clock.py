"""TradingClock: the single answer to "may the bot do X in market M right now?".

Three gates, from strictest to loosest:
  can_propose  -> new trade idea sent to Telegram (needs market + time rules + you available)
  can_execute  -> approved entry sent to the exchange (market + time rules; you just approved)
  can_exit     -> protective exits / square-off (only needs the market to be open)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from tradebot.config import AppConfig
from tradebot.core.models import Horizon, Market
from tradebot.timeutil import IST
from tradebot.timing.crypto import CryptoClock
from tradebot.timing.nse import MARKET_CLOSE, CalendarUnavailable, NseCalendar, NsePhase
from tradebot.timing.windows import hhmm_to_minutes


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()     # non-blocking context (expiry day, low liquidity, ...)

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class MarketStatus:
    market: Market
    is_open: bool
    phase: str
    detail: str
    notes: list[str] = field(default_factory=list)
    next_event: str | None = None


def _t(v: str) -> time:
    m = hhmm_to_minutes(v)
    return time(m // 60, m % 60)


def _shift(t: time, minutes: int) -> time:
    return (datetime.combine(datetime.min, t) + timedelta(minutes=minutes)).time()


class TradingClock:
    def __init__(self, cfg: AppConfig, nse: NseCalendar | None = None):
        self.cfg = cfg
        self.t = cfg.timing
        self.nse = nse or NseCalendar.from_yaml(self.t.nse.holidays_file)
        self.crypto = CryptoClock(self.t.crypto)

    # ---------- you ----------

    def user_available(self, at: datetime) -> bool:
        windows = self.t.availability_ist
        return not windows or any(w.contains(at.astimezone(IST)) for w in windows)

    # ---------- status ----------

    def status(self, market: Market, at: datetime) -> MarketStatus:
        if market is Market.CRYPTO_DELTA:
            notes = []
            if self.crypto.in_funding_blackout(at):
                notes.append("funding blackout")
            if self.crypto.is_low_liquidity(at):
                notes.append("low-liquidity window")
            nf = self.crypto.next_funding(at)
            return MarketStatus(market, True, "open_24x7", "crypto trades 24/7", notes,
                                f"next funding {nf.astimezone(IST):%a %d %b %H:%M} IST")

        local = at.astimezone(IST)
        _, reason = self.nse.day_status(local.date())
        phase = self.nse.phase(at)
        if phase is not NsePhase.OPEN and reason == "trading day":
            reason = "outside session hours (09:15-15:30)"
        notes = [f"{u} {c} expiry" for u, c in self._expiring(local.date())]
        status = MarketStatus(market, phase is NsePhase.OPEN, phase.value, reason, notes)
        bounds = self.nse.session_bounds(local.date())
        if phase is NsePhase.OPEN and bounds:
            mins = int((datetime.combine(local.date(), bounds[1], IST) - local).total_seconds() // 60)
            status.next_event = f"close in {mins} min"
        return status

    def _expiring(self, d) -> list[tuple[str, str]]:
        try:
            return self.nse.expiring_on(self.t.nse.expiries, d)
        except CalendarUnavailable:  # unknown year: status still renders, gates stay closed
            return []

    # ---------- gates ----------

    def can_propose(self, market: Market, horizon: Horizon, at: datetime,
                    allow_open_close_volatility: bool = False) -> GateDecision:
        d = self.can_execute(market, horizon, at, allow_open_close_volatility)
        if not self.user_available(at):
            return GateDecision(False, d.reasons + ("quiet hours: you are marked unavailable",),
                                d.notes)
        return d

    def can_execute(self, market: Market, horizon: Horizon, at: datetime,
                    allow_open_close_volatility: bool = False) -> GateDecision:
        st = self.status(market, at)
        reasons: list[str] = []

        if market is Market.CRYPTO_DELTA:
            if "funding blackout" in st.notes:
                reasons.append(f"within {self.t.crypto.funding_blackout_minutes} min of funding")
            if "low-liquidity window" in st.notes and self.t.crypto.block_new_entries_in_low_liquidity:
                reasons.append("low-liquidity window")
            return GateDecision(not reasons, tuple(reasons), tuple(st.notes))

        if not st.is_open:
            return GateDecision(False, (f"NSE {st.phase}: {st.detail}",), tuple(st.notes))

        local = at.astimezone(IST)
        open_t, close_t = self.nse.session_bounds(local.date())
        now = local.time()
        n = self.t.nse
        regular = (open_t, close_t) == (time(9, 15), MARKET_CLOSE)
        if regular and not allow_open_close_volatility:
            if now < _shift(open_t, n.avoid_open_minutes):
                reasons.append(f"first {n.avoid_open_minutes} min of session")
            if now >= _shift(close_t, -n.avoid_close_minutes):
                reasons.append(f"last {n.avoid_close_minutes} min of session")
        if horizon is Horizon.INTRADAY and regular and now >= _t(n.no_new_intraday_after):
            reasons.append(f"no new intraday entries after {n.no_new_intraday_after}")
        return GateDecision(not reasons, tuple(reasons), tuple(st.notes))

    def can_exit(self, market: Market, at: datetime) -> GateDecision:
        st = self.status(market, at)
        if st.is_open:
            return GateDecision(True)
        return GateDecision(False, (f"NSE {st.phase}: exit will be queued for next open",))

    def square_off_due(self, at: datetime) -> bool:
        """True from square_off_at until close on NSE trading days (intraday positions only)."""
        local = at.astimezone(IST)
        bounds = self.nse.session_bounds(local.date())
        if not bounds or not self.status(Market.NSE, at).is_open:
            return False
        regular = bounds == (time(9, 15), MARKET_CLOSE)
        cutoff = _t(self.t.nse.square_off_at) if regular else _shift(bounds[1], -10)
        return local.time() >= cutoff
