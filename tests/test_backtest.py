from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tradebot.backtest import metrics as M
from tradebot.backtest.execution import ExecData, simulate_trade
from tradebot.backtest.portfolio import SignalEvent, run_portfolio
from tradebot.backtest.walkforward import combos, make_folds
from tradebot.config import CryptoCosts, load_config
from tradebot.core.models import Horizon, Instrument, Market, Side, Signal
from tradebot.risk.engine import CryptoRiskEngine
from tradebot.risk.events import EventCalendar
from tradebot.risk.sizing import ContractSpec
from tradebot.timing.clock import TradingClock

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 3, 2, 6, 0, tzinfo=UTC)      # Mon 11:30 IST: inside availability, no funding
NO_COST = CryptoCosts(taker_fee_pct=0, maker_fee_pct=0, gst_pct=0, slippage_pct=0)
COSTS = CryptoCosts()
BTC = ContractSpec("BTCUSD", 27, 0.001, 0.5, 0.25, 125000)


def ex_from(prices: list[tuple[float, float, float, float]], start=T0, funding=()):
    ts = np.array([np.datetime64(pd.Timestamp(start).tz_localize(None) + pd.Timedelta(minutes=5 * i), "ns")
                   for i in range(len(prices))])
    o, h, lo, c = (np.array(x, float) for x in zip(*prices, strict=True))
    fts = np.array([np.datetime64(pd.Timestamp(t).tz_convert("UTC").tz_localize(None), "ns") for t, _ in funding],
                   dtype="datetime64[ns]")
    return ExecData("BTCUSD", ts, o, h, lo, c, fts, np.array([r for _, r in funding], float))


FLAT = [(100, 100.5, 99.5, 100)] * 3


# ---------- execution sim ----------

def test_fill_after_delay_and_target_with_maker_fee():
    ex = ex_from(FLAT + [(100, 101, 99.8, 100.8), (101, 106, 100.5, 105)] + FLAT)
    s = simulate_trade(ex, Side.BUY, T0, 100, 98, 104, timedelta(hours=1), NO_COST,
                       delay=timedelta(minutes=12), max_drift_pct=0.5)
    assert s.entry_ts == T0 + timedelta(minutes=15)       # first 5m bar at/after T0+12m
    assert (s.reason, s.exit_px, s.gross_pu) == ("target", 104, 4) and s.net_r == pytest.approx(2.0)
    s2 = simulate_trade(ex, Side.BUY, T0, 100, 98, 104, timedelta(hours=1), COSTS,
                        delay=timedelta(minutes=12), max_drift_pct=0.5)
    assert s2.fees_pu == pytest.approx(s2.entry_px * 0.00059 + 104 * 0.000236)
    assert s2.entry_px == pytest.approx(100 * 1.0002) and s2.net_r < 2.0


def test_stop_first_gap_and_time_exit():
    both = ex_from([(100, 100, 100, 100), (100, 105, 97, 100)])
    s = simulate_trade(both, Side.BUY, T0, 100, 98, 104, timedelta(hours=1), NO_COST, timedelta(0), 1)
    assert s.reason == "stop" and s.net_r == pytest.approx(-1)
    gap = ex_from([(100, 100.2, 99.9, 100), (95, 96, 94, 95)])
    s = simulate_trade(gap, Side.BUY, T0, 100, 98, 104, timedelta(hours=1), NO_COST, timedelta(0), 1)
    assert s.reason == "stop(gap)" and s.exit_px == 95 and s.net_r == pytest.approx(-2.5)
    drift = ex_from([(100, 100, 100, 100)] * 30)
    s = simulate_trade(drift, Side.SELL, T0, 100, 102, 96, timedelta(minutes=30), NO_COST, timedelta(0), 1)
    assert s.reason == "time" and s.exit_ts == T0 + timedelta(minutes=30)


def test_drift_and_beyond_stop_skip():
    ex = ex_from([(101, 101, 101, 101)] * 3)
    assert simulate_trade(ex, Side.BUY, T0, 100, 98, 104, timedelta(hours=1), NO_COST,
                          timedelta(0), 0.5).reason == "price drift"
    ex = ex_from([(97.9, 98, 97, 97.5)] * 3)
    assert simulate_trade(ex, Side.BUY, T0, 98.2, 98, 104, timedelta(hours=1), NO_COST,
                          timedelta(0), 1).reason == "beyond stop at approval"


def test_funding_charged_on_settlements_crossed():
    settle = T0 + timedelta(minutes=10)
    ex = ex_from([(100, 100.2, 99.9, 100)] * 6, funding=[(settle, 0.05), (T0 - timedelta(hours=1), 9.9)])
    long_ = simulate_trade(ex, Side.BUY, T0, 100, 90, 120, timedelta(minutes=30), NO_COST, timedelta(0), 1)
    short = simulate_trade(ex, Side.SELL, T0, 100, 110, 80, timedelta(minutes=30), NO_COST, timedelta(0), 1)
    assert long_.funding_pu == pytest.approx(0.05) and short.funding_pu == pytest.approx(-0.05)


# ---------- portfolio + live risk engine ----------

@pytest.fixture
def cfg():
    cfg, _ = load_config(ROOT / "config", env_file=None)
    return cfg


def sig(t, entry=100.0, stop=98.0, target=104.0):
    return Signal(Instrument(Market.CRYPTO_DELTA, "BTCUSD"), Side.BUY, "test", Horizon.SWING,
                  Decimal(str(entry)), Decimal(str(stop)), Decimal(str(target)), 0.7, "r", t,
                  max_hold=timedelta(hours=2))


def engine(cfg):
    return CryptoRiskEngine(cfg, {"BTCUSD": BTC}, TradingClock(cfg), EventCalendar([]))


def test_portfolio_sizes_via_risk_engine_and_blocks_second_position(cfg):
    bars = [(100, 100.2, 99.9, 100)] * 4 + [(100, 104.5, 99.9, 104)] + [(104, 104, 104, 104)] * 30
    ex = ex_from(bars)
    evs = [SignalEvent(sig(T0), T0, "trend_up"), SignalEvent(sig(T0 + timedelta(minutes=5)),
                                                             T0 + timedelta(minutes=5), "trend_up")]
    res = run_portfolio(cfg, engine(cfg), evs, {"BTCUSD": ex}, timedelta(minutes=3))
    assert len(res.trades) == 1
    t = res.trades[0]
    # equity ₹1,00,000 / 88 = $1136; 1% = $11.36; 2 * 0.001 per contract -> 5681 contracts... capped
    assert t.contracts >= 1 and t.risk_inr <= 1000 + 1e-6   # sizing includes slippage + costs
    assert any("already 1 position" in b[3] for b in res.blocked)
    assert t.pnl_inr > 0 and res.equity_points[-1][1] == pytest.approx(100_000 + t.pnl_inr)


def test_portfolio_triggers_cooldown_after_losses(cfg):
    cfg = cfg.model_copy(update={"risk": cfg.risk.model_copy(update={"max_risk_per_trade_pct": 0.5})})
    events, bars = [], []
    for k in range(4):                      # 4 losing trades, 1 hour apart
        bars += [(100, 100.1, 99.9, 100)] + [(100, 100, 97, 97.5)] + [(98, 98, 98, 98)] * 10
        t = T0 + timedelta(minutes=30, hours=k)   # :30 past, clear of the 08:00 funding blackout
        events.append(SignalEvent(sig(t), t, "trend_up"))
    ex = ex_from(bars, start=T0 + timedelta(minutes=30))
    res = run_portfolio(cfg, engine(cfg), events, {"BTCUSD": ex}, timedelta(0))
    assert len(res.trades) == 3                              # 4th blocked by 3-loss cooldown
    assert any("losses in a row" in b[3] for b in res.blocked)
    assert res.halts and res.halts[0].kind.value == "cooldown"


# ---------- metrics + walk-forward plumbing ----------

def test_curve_stats_and_tax():
    eq = pd.Series([100, 110, 99, 120], index=pd.date_range("2026-01-01", periods=4, tz="UTC"))
    st = M.curve_stats(eq)
    assert st["max_DD_%"] == pytest.approx(-10.0) and st["total_return_%"] == 20.0
    df = pd.DataFrame({"pnl_inr": [1000.0, -800.0]})
    tax = M.tax_estimate(df)
    assert tax["tax_if_VDA_inr"] == round(1000 * 0.312) and tax["tax_if_business_inr"] == round(200 * 0.312)


def test_folds_and_grids():
    f = make_folds(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC), 180, 60)
    assert len(f) == 3 and f[0].test[0] == f[0].train[1]
    assert f[1].train[0] == f[0].train[0] + timedelta(days=60)
    assert len(combos("donchian_breakout")) == 18 and combos("unknown") == [{}]


def test_stop_out_loss_stays_within_budget_all_in(cfg):
    """With costs in the sizing, a full stop-out (fees, GST, slippage) costs <= 1% of equity."""
    bars = [(100, 100.1, 99.9, 100), (100, 100, 97, 97.5)] + [(98, 98, 98, 98)] * 5
    res = run_portfolio(cfg, engine(cfg), [SignalEvent(sig(T0), T0, "trend_up")],
                        {"BTCUSD": ex_from(bars)}, timedelta(0))
    assert -1000 <= res.trades[0].pnl_inr < -900
