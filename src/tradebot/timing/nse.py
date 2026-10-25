"""NSE trading calendar: sessions, holidays, special sessions, F&O expiry dates. All times IST."""

from __future__ import annotations

import calendar as _cal
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path

import yaml

from tradebot.config import ExpiryRule
from tradebot.timeutil import IST
from tradebot.timing.windows import hhmm_to_minutes

PRE_OPEN_START = time(9, 0)
PRE_OPEN_MATCH = time(9, 8)    # order entry closes, matching starts
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
POST_CLOSE_END = time(16, 0)


class NsePhase(StrEnum):
    CLOSED = "closed"
    PRE_OPEN = "pre_open"                  # 09:00-09:08 order collection
    PRE_OPEN_MATCHING = "pre_open_matching"  # 09:08-09:15 matching / buffer, no orders
    OPEN = "open"                          # 09:15-15:30 continuous trading
    POST_CLOSE = "post_close"              # 15:30-16:00 closing price / post-close session


class CalendarUnavailable(RuntimeError):
    """The holiday list for this year isn't loaded, so trading days can't be known."""


@dataclass(frozen=True)
class SpecialSession:
    day: date
    start: time
    end: time
    name: str


def _hhmm(v: str) -> time:
    m = hhmm_to_minutes(v)
    return time(m // 60, m % 60)


class NseCalendar:
    def __init__(self, holidays: dict[date, str], special: list[SpecialSession] | None = None):
        self.holidays = holidays
        self.special = {s.day: s for s in (special or [])}
        self.years = {d.year for d in holidays}

    @classmethod
    def from_yaml(cls, path: Path | str) -> NseCalendar:
        raw = yaml.safe_load(Path(path).read_text()) or {}
        holidays: dict[date, str] = {}
        for key, rows in raw.items():
            if isinstance(key, int):
                for r in rows or []:
                    if r["date"].year != key:
                        raise ValueError(f"{r['date']} listed under year {key}")
                    holidays[r["date"]] = r["name"]
        special = [SpecialSession(s["date"], _hhmm(s["start"]), _hhmm(s["end"]), s["name"])
                   for s in raw.get("special_sessions") or []]
        cal = cls(holidays, special)
        cal.years |= {k for k in raw if isinstance(k, int)}  # a year with zero holidays still counts
        return cal

    # ---------- days ----------

    def _require_year(self, d: date) -> None:
        if d.year not in self.years:
            raise CalendarUnavailable(
                f"NSE holiday list for {d.year} not loaded; add it to the holidays file")

    def day_status(self, d: date) -> tuple[bool, str]:
        """(is_trading_day, reason). Never raises: unknown years are reported as closed."""
        if d in self.special:
            return True, f"special session: {self.special[d].name}"
        if d.weekday() >= 5:
            return False, "weekend"
        if d.year not in self.years:
            return False, f"holiday calendar for {d.year} not loaded (fail-safe: closed)"
        if d in self.holidays:
            return False, f"holiday: {self.holidays[d]}"
        return True, "trading day"

    def is_trading_day(self, d: date) -> bool:
        return self.day_status(d)[0]

    def prev_trading_day(self, d: date) -> date:
        """Latest trading day on or before d. Raises CalendarUnavailable for unknown years."""
        for _ in range(15):
            self._require_year(d)
            if self.is_trading_day(d):
                return d
            d -= timedelta(days=1)
        raise RuntimeError("no trading day in 15 days; holiday file looks wrong")

    def next_trading_day(self, d: date) -> date:
        """Earliest trading day strictly after d."""
        for _ in range(15):
            d += timedelta(days=1)
            self._require_year(d)
            if self.is_trading_day(d):
                return d
        raise RuntimeError("no trading day in 15 days; holiday file looks wrong")

    # ---------- intraday ----------

    def session_bounds(self, d: date) -> tuple[time, time] | None:
        """Continuous-trading open/close for d, or None if closed."""
        if d in self.special:
            return self.special[d].start, self.special[d].end
        return (MARKET_OPEN, MARKET_CLOSE) if self.is_trading_day(d) else None

    def phase(self, at: datetime) -> NsePhase:
        local = at.astimezone(IST)
        d, t = local.date(), local.time()
        if d in self.special:
            s = self.special[d]
            return NsePhase.OPEN if s.start <= t < s.end else NsePhase.CLOSED
        if not self.is_trading_day(d):
            return NsePhase.CLOSED
        if PRE_OPEN_START <= t < PRE_OPEN_MATCH:
            return NsePhase.PRE_OPEN
        if PRE_OPEN_MATCH <= t < MARKET_OPEN:
            return NsePhase.PRE_OPEN_MATCHING
        if MARKET_OPEN <= t < MARKET_CLOSE:
            return NsePhase.OPEN
        if MARKET_CLOSE <= t < POST_CLOSE_END:
            return NsePhase.POST_CLOSE
        return NsePhase.CLOSED

    # ---------- F&O expiry ----------

    def _rule_dates(self, rule: ExpiryRule, year: int, month: int) -> list[date]:
        wd = rule.weekday.index
        days = [date(year, month, d) for d in range(1, _cal.monthrange(year, month)[1] + 1)
                if date(year, month, d).weekday() == wd]
        nominal = days if rule.cycle == "weekly" else days[-1:]
        return [self.prev_trading_day(n) for n in nominal]  # holiday -> previous trading day

    def expiries(self, rules: list[ExpiryRule], underlying: str, year: int, month: int
                 ) -> list[tuple[date, str]]:
        out = {(d, r.cycle) for r in rules if r.underlying == underlying
               for d in self._rule_dates(r, year, month)}
        return sorted(out)

    def next_expiry(self, rules: list[ExpiryRule], underlying: str, on_or_after: date
                    ) -> tuple[date, str] | None:
        y, m = on_or_after.year, on_or_after.month
        for _ in range(3):
            for d, cycle in self.expiries(rules, underlying, y, m):
                if d >= on_or_after:
                    return d, cycle
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return None

    def expiring_on(self, rules: list[ExpiryRule], d: date) -> list[tuple[str, str]]:
        """[(underlying, cycle)] expiring on d."""
        nxt = d + timedelta(days=7)  # a holiday shift can pull next month's first expiry back
        months = {(d.year, d.month), (nxt.year, nxt.month)}
        return sorted({(r.underlying, r.cycle) for r in rules for y, m in months
                       if d in self._rule_dates(r, y, m)})
