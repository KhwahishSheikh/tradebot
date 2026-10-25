from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tradebot.config import AppConfig, load_config
from tradebot.core.models import Horizon, Market
from tradebot.timeutil import IST
from tradebot.timing.clock import TradingClock
from tradebot.timing.nse import CalendarUnavailable, NseCalendar, NsePhase
from tradebot.timing.windows import TimeWindow

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    cfg, _ = load_config(ROOT / "config", env_file=None)
    return cfg


@pytest.fixture(scope="module")
def clock(cfg) -> TradingClock:
    return TradingClock(cfg, NseCalendar.from_yaml(ROOT / "config/calendars/nse_holidays.yaml"))


def ist(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=IST)


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


# ---------- NSE days & phases ----------

@pytest.mark.parametrize("d,trading,why", [
    ("2026-10-16", True, "trading day"),      # Friday
    ("2026-10-17", False, "weekend"),
    ("2026-10-02", False, "Gandhi"),
    ("2026-10-20", False, "Dussehra"),
    ("2027-01-04", False, "not loaded"),      # unknown year -> fail-safe closed
])
def test_day_status(clock, d, trading, why):
    ok, reason = clock.nse.day_status(date.fromisoformat(d))
    assert ok is trading and why in reason


@pytest.mark.parametrize("t,phase", [
    ("08:59", NsePhase.CLOSED), ("09:03", NsePhase.PRE_OPEN), ("09:10", NsePhase.PRE_OPEN_MATCHING),
    ("09:15", NsePhase.OPEN), ("15:29", NsePhase.OPEN), ("15:30", NsePhase.POST_CLOSE),
    ("16:00", NsePhase.CLOSED),
])
def test_phases(clock, t, phase):
    assert clock.nse.phase(ist(f"2026-10-16T{t}")) is phase


def test_utc_input_is_converted(clock):
    assert clock.nse.phase(utc("2026-10-16T04:00")) is NsePhase.OPEN  # 09:30 IST


# ---------- expiry ----------

def test_weekly_expiry_shifts_back_on_holiday(cfg, clock):
    # Tue 20 Oct 2026 is Dussehra -> NIFTY weekly expires Mon 19 Oct
    assert clock.nse.next_expiry(cfg.timing.nse.expiries, "NIFTY", date(2026, 10, 17)) == (
        date(2026, 10, 19), "weekly")


def test_monthly_expiry_last_tuesday(cfg, clock):
    exp = clock.nse.expiries(cfg.timing.nse.expiries, "BANKNIFTY", 2026, 10)
    assert exp == [(date(2026, 10, 27), "monthly")]
    assert clock.nse.expiring_on(cfg.timing.nse.expiries, date(2026, 10, 27)) == [
        ("BANKNIFTY", "monthly"), ("NIFTY", "monthly"), ("NIFTY", "weekly"), ("STOCKS", "monthly")]


def test_monthly_expiry_on_holiday(cfg, clock):
    # Last Tue of Nov 2026 = 24th = Guru Nanak Jayanti -> Mon 23rd
    assert clock.nse.expiries(cfg.timing.nse.expiries, "BANKNIFTY", 2026, 11) == [
        (date(2026, 11, 23), "monthly")]


def test_expiry_in_unknown_year_raises(cfg, clock):
    with pytest.raises(CalendarUnavailable):
        clock.nse.expiries(cfg.timing.nse.expiries, "NIFTY", 2027, 2)


# ---------- gates ----------

def test_intraday_volatility_windows(clock):
    assert not clock.can_propose(Market.NSE, Horizon.INTRADAY, ist("2026-10-16T09:20"))
    assert clock.can_propose(Market.NSE, Horizon.INTRADAY, ist("2026-10-16T09:20"),
                             allow_open_close_volatility=True)
    assert clock.can_propose(Market.NSE, Horizon.INTRADAY, ist("2026-10-16T09:35"))
    late = clock.can_propose(Market.NSE, Horizon.INTRADAY, ist("2026-10-16T14:50"))
    assert not late and "14:45" in late.reasons[0]
    assert clock.can_propose(Market.NSE, Horizon.SWING, ist("2026-10-16T14:50"))
    assert not clock.can_propose(Market.NSE, Horizon.SWING, ist("2026-10-16T15:20"))


def test_closed_market_blocks_entries_and_exits(clock):
    at = ist("2026-10-20T11:00")  # Dussehra
    assert not clock.can_propose(Market.NSE, Horizon.SWING, at)
    assert not clock.can_exit(Market.NSE, at)
    assert clock.can_exit(Market.CRYPTO_DELTA, at)


def test_quiet_hours_block_proposals_not_execution_or_exits(clock):
    at = ist("2026-10-16T23:00")  # crypto open, you're unavailable
    assert not clock.can_propose(Market.CRYPTO_DELTA, Horizon.SWING, at)
    assert clock.can_execute(Market.CRYPTO_DELTA, Horizon.SWING, at)
    assert clock.can_exit(Market.CRYPTO_DELTA, at)


def test_funding_blackout(clock):
    assert clock.crypto.next_funding(utc("2026-10-16T07:00")) == utc("2026-10-16T08:00")
    assert clock.crypto.next_funding(utc("2026-10-16T23:30")) == utc("2026-10-17T00:00")
    assert not clock.can_execute(Market.CRYPTO_DELTA, Horizon.SWING, utc("2026-10-16T07:57"))
    assert not clock.can_execute(Market.CRYPTO_DELTA, Horizon.SWING, utc("2026-10-16T08:03"))
    assert clock.can_execute(Market.CRYPTO_DELTA, Horizon.SWING, utc("2026-10-16T08:10"))


def test_square_off(clock):
    assert not clock.square_off_due(ist("2026-10-16T15:05"))
    assert clock.square_off_due(ist("2026-10-16T15:12"))
    assert not clock.square_off_due(ist("2026-10-17T15:12"))  # weekend


def test_overnight_window():
    w = TimeWindow(days=["fri"], start="22:00", end="02:00")
    assert w.contains(datetime(2026, 10, 16, 23, 0))   # Fri 23:00
    assert w.contains(datetime(2026, 10, 17, 1, 59))   # Sat 01:59
    assert not w.contains(datetime(2026, 10, 17, 2, 0))
    assert not w.contains(datetime(2026, 10, 15, 23, 0))  # Thu


def test_special_session_opens_closed_day(cfg):
    from datetime import time

    from tradebot.timing.nse import SpecialSession
    cal = NseCalendar({date(2026, 1, 26): "x"}, [
        SpecialSession(date(2026, 11, 8), time(18), time(19), "Muhurat")])
    cal.years.add(2026)
    c = TradingClock(cfg, cal)
    assert c.status(Market.NSE, ist("2026-11-08T18:30")).is_open
    assert not c.status(Market.NSE, ist("2026-11-08T19:00")).is_open
    assert c.square_off_due(ist("2026-11-08T18:52"))
