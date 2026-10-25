from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tradebot.config import AppConfig, load_config
from tradebot.core.interfaces import RiskStage
from tradebot.core.models import Horizon, Instrument, Market, Side, Signal
from tradebot.risk.engine import CryptoRiskEngine, MarketContext
from tradebot.risk.events import EventCalendar, MacroEvent
from tradebot.risk.manager import RiskManager
from tradebot.risk.sizing import ContractSpec, liquidation_price, size_position
from tradebot.risk.state import HaltKind, OpenRisk, PortfolioState, RiskStateStore
from tradebot.timing.clock import TradingClock

ROOT = Path(__file__).resolve().parents[1]
BTC = ContractSpec("BTCUSD", 27, 0.001, 0.5, 0.25, 125000)
ETH = ContractSpec("ETHUSD", 3136, 0.01, 0.05, 0.25, 162683)
NOW = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)            # Fri 11:30 IST, no funding window


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    cfg, _ = load_config(ROOT / "config", env_file=None)
    return cfg


def engine(cfg, events=()):
    return CryptoRiskEngine(cfg, {"BTCUSD": BTC, "ETHUSD": ETH}, TradingClock(cfg),
                            EventCalendar(list(events) or [MacroEvent(
                                "far", NOW + timedelta(days=5), ("crypto_delta",),
                                timedelta(hours=1), timedelta(hours=1))]))


def sig(side=Side.BUY, entry=80_000.0, stop=79_000.0, target=82_000.0, sym="BTCUSD"):
    return Signal(Instrument(Market.CRYPTO_DELTA, sym), side, "test", Horizon.SWING,
                  Decimal(str(entry)), Decimal(str(stop)), Decimal(str(target)), 0.7, "why", NOW)


def state(equity=100_000.0, day_start=None, peak=None, **kw):
    return PortfolioState(equity, day_start or equity, peak or equity, **kw)


CTX = MarketContext(NOW, live_price=80_000.0, last_bar_close=NOW - timedelta(minutes=10))


# ---------- sizing ----------

def test_size_from_stop_distance_rounds_down(cfg):
    # ₹1,00,000 / 88 = $1136.36; 1% = $11.36; risk per contract = 1000 * 0.001 = $1 -> 11
    s = size_position(BTC, Side.BUY, 80_000, 79_000, equity_inr=100_000, usd_inr=88,
                      risk_pct=1.0, order_leverage=3, max_notional_usd=1e9, max_risk_usd=1e9)
    assert s.contracts == 11 and s.risk_pct <= 1.0 and s.limited_by == "risk per trade"
    assert s.notional_usd == pytest.approx(880) and s.margin_usd == pytest.approx(880 / 3)


def test_size_zero_when_stop_too_wide_for_capital():
    res = size_position(BTC, Side.BUY, 80_000, 60_000, equity_inr=50_000, usd_inr=88,
                        risk_pct=1.0, order_leverage=3, max_notional_usd=1e9, max_risk_usd=1e9)
    assert isinstance(res, str) and "one contract would risk" in res


def test_liquidation_price():
    assert liquidation_price(Side.BUY, 90_000, 3, 0.25) == pytest.approx(90_000 * (1 - 1 / 3 + 0.0025))
    assert liquidation_price(Side.SELL, 90_000, 3, 0.25) > 90_000


# ---------- engine ----------

def test_happy_path_both_stages(cfg):
    e = engine(cfg)
    for stage in RiskStage:
        d = e.assess(sig(), state(), stage, CTX)
        assert d.allowed, d.reasons
        # (1000 stop + 80000 x 0.158% round-trip costs) x 0.001 = $1.126/contract -> 10
        assert d.sizing.contracts == 10


def test_execution_resizes_on_live_price_and_blocks_drift(cfg):
    e = engine(cfg)
    d = e.assess(sig(), state(), RiskStage.EXECUTION,
                 MarketContext(NOW, 80_200.0, CTX.last_bar_close))     # +0.25%: ok
    assert d.allowed and d.sizing.entry == 80_200 and d.sizing.contracts == 8   # wider stop now
    d = e.assess(sig(), state(), RiskStage.EXECUTION,
                 MarketContext(NOW, 80_800.0, CTX.last_bar_close))     # +1%: too far
    assert not d.allowed and any("drifted" in r for r in d.reasons)
    d = e.assess(sig(), state(), RiskStage.EXECUTION, MarketContext(NOW, None, CTX.last_bar_close))
    assert not d.allowed and "no live price at execution" in d.reasons


def test_bad_stop_and_missing_stop_blocked(cfg):
    e = engine(cfg)
    assert not e.assess(sig(stop=81_000), state(), RiskStage.PROPOSAL, CTX).allowed
    assert not e.assess(sig(side=Side.SELL), state(), RiskStage.PROPOSAL, CTX).allowed
    assert any("missing stop" in r for r in e.assess(sig(stop=0), state(), RiskStage.PROPOSAL, CTX).reasons)


def test_daily_loss_and_drawdown_raise_halts(cfg):
    e = engine(cfg)
    d = e.assess(sig(), state(equity=97_000, day_start=100_000), RiskStage.PROPOSAL, CTX)
    assert not d.allowed and d.new_halts[0][0] is HaltKind.DAILY_LOSS
    assert d.new_halts[0][2] == datetime(2026, 10, 16, 18, 30, tzinfo=UTC)  # next IST midnight
    d = e.assess(sig(), state(equity=89_000, day_start=89_500, peak=100_000), RiskStage.PROPOSAL, CTX)
    assert not d.allowed and d.new_halts[0][0] is HaltKind.DRAWDOWN and d.new_halts[0][2] is None


def test_consecutive_losses_cooldown(cfg):
    e = engine(cfg)
    losses = [(NOW - timedelta(hours=h), -500.0) for h in (1, 2, 3)]
    d = e.assess(sig(), state(recent_results=losses), RiskStage.PROPOSAL, CTX)
    assert not d.allowed and d.new_halts[0][0] is HaltKind.COOLDOWN
    old = [(NOW - timedelta(hours=h), -500.0) for h in (30, 31, 32)]          # cooldown over
    assert e.assess(sig(), state(recent_results=old), RiskStage.PROPOSAL, CTX).allowed
    broken = [(NOW - timedelta(hours=1), -1.0), (NOW - timedelta(hours=2), 5.0),
              (NOW - timedelta(hours=3), -1.0), (NOW - timedelta(hours=4), -1.0)]
    assert e.assess(sig(), state(recent_results=broken), RiskStage.PROPOSAL, CTX).allowed


def test_exposure_limits(cfg):
    e = engine(cfg)
    btc_pos = OpenRisk("BTCUSD", "buy", 10, 80_000, 79_000, 0.001)
    d = e.assess(sig(), state(open_positions=[btc_pos]), RiskStage.PROPOSAL, CTX)
    assert not d.allowed and any("already 1 position" in r for r in d.reasons)
    # ETH while BTC open: heat cap 3% leaves room, so allowed but maybe reduced
    eth = sig(sym="ETHUSD", entry=2_400, stop=2_350, target=2_500)
    hot = [OpenRisk("BTCUSD", "buy", 6, 80_000, 75_000, 0.001)]   # $30 risk = 2.64% heat, 0.42x gross
    d = e.assess(eth, state(open_positions=hot), RiskStage.PROPOSAL, CTX)
    assert d.allowed and d.sizing.limited_by == "portfolio heat"
    assert d.sizing.risk_usd <= 1136.36 * 0.03 - 30 + 1e-6
    many = [OpenRisk(s, "buy", 1, 100, 99, 0.001) for s in ("A", "B", "C")]
    assert not e.assess(eth, state(open_positions=many), RiskStage.PROPOSAL, CTX).allowed


def test_event_blackout_and_stale_data(cfg):
    fomc = MacroEvent("FOMC", NOW + timedelta(minutes=30), ("crypto_delta",),
                      timedelta(hours=2), timedelta(hours=2))
    d = engine(cfg, [fomc]).assess(sig(), state(), RiskStage.PROPOSAL, CTX)
    assert not d.allowed and any("FOMC" in r for r in d.reasons)
    stale = MarketContext(NOW, 80_000.0, NOW - timedelta(hours=5))
    assert any("stale data" in r for r in engine(cfg).assess(sig(), state(), RiskStage.PROPOSAL, stale).reasons)


def test_timing_gate_applies(cfg):
    funding_time = MarketContext(datetime(2026, 10, 16, 8, 2, tzinfo=UTC), 80_000.0,
                                 datetime(2026, 10, 16, 8, 0, tzinfo=UTC))
    d = engine(cfg).assess(sig(), state(), RiskStage.EXECUTION, funding_time)
    assert not d.allowed and any("funding" in r for r in d.reasons)


# ---------- persistence + manager ----------

def test_state_store_and_halts_persist(tmp_path, cfg):
    path = tmp_path / "risk.db"
    st = RiskStateStore(path, 100_000)
    st.mark_equity(100_000, NOW - timedelta(days=2))
    st.mark_equity(104_000, NOW - timedelta(days=1))   # peak, and yesterday's close
    st.mark_equity(103_000, NOW)                         # -0.96% today: inside the daily limit
    s = st.state(at=NOW)
    assert (s.equity_inr, s.day_start_equity_inr, s.peak_equity_inr) == (103_000, 104_000, 104_000)

    mgr = RiskManager(engine(cfg), st, None)
    mgr.kill_switch("test", by="pytest")
    st.close()
    st2 = RiskStateStore(path, 100_000)                    # "restart"
    mgr2 = RiskManager(engine(cfg), st2, None)
    d = mgr2.check(sig(), RiskStage.PROPOSAL, CTX)
    assert not d.allowed and any("kill_switch" in r for r in d.reasons)
    assert mgr2.resume("pytest") == 1
    assert mgr2.check(sig(), RiskStage.PROPOSAL, CTX).allowed


def test_daily_halt_expires_at_ist_midnight(tmp_path):
    st = RiskStateStore(tmp_path / "r.db", 100_000)
    st.halt(HaltKind.DAILY_LOSS, "x", until=datetime(2026, 10, 16, 18, 30, tzinfo=UTC), at=NOW)
    assert st.active_halts(datetime(2026, 10, 16, 18, 0, tzinfo=UTC))
    assert not st.active_halts(datetime(2026, 10, 16, 18, 31, tzinfo=UTC))


def test_first_day_loss_counts_against_daily_limit(tmp_path):
    st = RiskStateStore(tmp_path / "r.db", 100_000)
    st.mark_equity(97_000, NOW)                      # first ever mark, already -3% today
    s = st.state(at=NOW)
    assert s.day_start_equity_inr == 100_000 and s.day_pnl_pct == pytest.approx(-3.0)
