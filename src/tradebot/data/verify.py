"""Cross-checks of the timing engine's assumptions against live exchange data.

Run daily: expiry rules and funding schedules have changed before and will change again.
A failed check should block proposals for that market until a human looks (wired in Ticket 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from tradebot.config import ExpiryRule
from tradebot.markets.angel.instruments import AngelInstruments
from tradebot.markets.delta.client import DeltaClient
from tradebot.timing.crypto import CryptoClock
from tradebot.timing.nse import CalendarUnavailable, NseCalendar


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def check_funding_schedule(delta: DeltaClient, clock: CryptoClock, symbol: str,
                           days: int = 7) -> CheckResult:
    changes = delta.funding_change_hours(symbol, days)
    if not changes:
        return CheckResult(f"funding schedule {symbol}", False, "no funding changes seen")
    off = [t for t in changes if clock.prev_funding(t) != t]
    hours = sorted({f"{t:%H:%M}" for t in changes})
    if off:
        return CheckResult(f"funding schedule {symbol}", False,
                           f"changes at unexpected times {[f'{t:%m-%d %H:%M}' for t in off[:5]]} UTC")
    return CheckResult(f"funding schedule {symbol}", True,
                       f"{len(changes)} changes in {days}d, all at {hours} UTC as configured")


def check_expiries(cal: NseCalendar, rules: list[ExpiryRule], inst: AngelInstruments,
                   underlying: str, today: date, instrument_type: str = "OPTIDX",
                   horizon_days: int = 28) -> CheckResult:
    listed = [d for d in inst.expiries(underlying, instrument_type) if d >= today]
    listed_in_cal = [d for d in listed if d.year in cal.years]
    predicted: set[date] = set()
    try:
        for d in listed_in_cal + [today + timedelta(days=i) for i in range(horizon_days)]:
            predicted |= {e for e, _ in cal.expiries(rules, underlying, d.year, d.month)}
    except CalendarUnavailable as e:
        return CheckResult(f"expiries {underlying}", False, str(e))

    unexpected = sorted(set(listed_in_cal) - predicted)      # exchange has it, we don't
    near = {d for d in predicted if today <= d < today + timedelta(days=horizon_days)}
    missing = sorted(near - set(listed))                      # we expect it soon, exchange doesn't
    if unexpected or missing:
        return CheckResult(f"expiries {underlying}", False,
                           f"exchange-only: {unexpected}  rules-only: {missing}")
    nxt = ", ".join(f"{d:%a %d %b}" for d in listed_in_cal[:4])
    return CheckResult(f"expiries {underlying}", True,
                       f"{len(listed_in_cal)} listed expiries match rules (next: {nxt})")
