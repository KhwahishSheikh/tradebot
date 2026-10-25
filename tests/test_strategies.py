from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from tradebot.config import AppConfig, StrategyEntry
from tradebot.core.models import Market, Side
from tradebot.data.store import MarketStore
from tradebot.strategies import indicators as ta
from tradebot.strategies.crypto import REGISTRY
from tradebot.strategies.engine import SignalEngine, resample_bars
from tradebot.strategies.features import Regime, build_features
from tradebot.strategies.funding import funding_impact
from tradebot.strategies.study import simulate

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def synth_bars(n=1500, seed=7, drift=0.0003, step="1h") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    r = drift + 0.006 * rng.standard_normal(n)
    r[600:900] += 0.002                       # a trending stretch
    close = 50_000 * np.exp(np.cumsum(r))
    open_ = np.r_[close[0], close[:-1]]
    spread = np.abs(rng.standard_normal(n)) * 0.003 * close
    return pd.DataFrame({
        "ts": pd.date_range(T0, periods=n, freq=step, tz=UTC), "open": open_,
        "high": np.maximum(open_, close) + spread, "low": np.minimum(open_, close) - spread,
        "close": close, "volume": rng.uniform(1e5, 1e6, n)})


def synth_funding(bars: pd.DataFrame, rate=0.01) -> pd.DataFrame:
    return pd.DataFrame({"ts": bars["ts"], "close": rate})


# ---------- no lookahead ----------

def test_features_are_causal():
    """Row i must be identical whether or not future bars exist."""
    bars = synth_bars()
    full = build_features(bars, synth_funding(bars), vol_window=500)
    for cut in (400, 777, 1200):
        part = build_features(bars.iloc[:cut], synth_funding(bars.iloc[:cut]), vol_window=500)
        a, b = full.iloc[:cut], part
        num = a.select_dtypes("number").columns
        pd.testing.assert_frame_equal(a[num], b[num], check_exact=False, rtol=1e-9)
        assert (a["regime"].to_numpy() == b["regime"].to_numpy()).all()


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_strategy_decisions_are_causal(name):
    bars = synth_bars(step="15min" if name == "vwap_intraday" else "1h")
    strat = REGISTRY[name]()
    full = strat.prepare(build_features(bars, synth_funding(bars), vol_window=500))
    cut = 1100
    part = strat.prepare(build_features(bars.iloc[:cut], synth_funding(bars.iloc[:cut]),
                                        vol_window=500))
    for i in range(strat.warmup, cut):
        assert strat.decide(full, i) == strat.decide(part, i), f"lookahead at bar {i}"


def test_donchian_excludes_current_bar():
    df = pd.DataFrame({"high": [1, 2, 3, 10], "low": [0, 0, 0, 0]})
    assert ta.donchian(df, 3)["dc_high"].iat[3] == 3   # not 10


def test_funding_merge_is_backward_only():
    bars = synth_bars(10)
    funding = pd.DataFrame({"ts": [bars["ts"].iat[5]], "close": [0.05]})
    f = build_features(bars, funding, vol_window=4)
    assert f["funding"].iloc[:5].isna().all() and (f["funding"].iloc[5:] == 0.05).all()


# ---------- pieces ----------

def test_resample_drops_incomplete_bucket():
    five = synth_bars(14, step="5min")       # 12 bars = one full hour, 2 left over
    out = resample_bars(five, "1h", "5m")
    assert len(out) == 1 and out["high"].iat[0] == five["high"].iloc[:12].max()


def test_funding_impact():
    long_crowded = funding_impact(Side.BUY, 0.07, 50_000, 500, hold_hours=24)
    assert long_crowded.confidence_delta == -0.20
    assert long_crowded.cost_r == pytest.approx(0.07 * 3 / 100 * 50_000 / 500)   # 0.21R
    short_paid = funding_impact(Side.SELL, 0.07, 50_000, 500, hold_hours=24)
    assert short_paid.cost_r < 0 and short_paid.confidence_delta == 0.05


def test_simulate_stop_first_when_both_hit_and_gap_fill():
    f = pd.DataFrame({"open": [100, 100, 100, 90], "high": [100, 104, 101, 91],
                      "low": [100, 96, 99, 89], "close": [100, 100, 100, 90]})
    j, px, why, _ = simulate(f, 0, Side.BUY, stop_dist=3, target_dist=3, max_bars=10)
    assert (j, px, why) == (1, 97, "stop")                # both inside bar 1 -> stop
    j, px, why, _ = simulate(f, 0, Side.BUY, stop_dist=5, target_dist=50, max_bars=10)
    assert (j, why, px) == (3, "stop(gap)", 90)           # opened below stop -> fill at open


# ---------- engine gates ----------

@pytest.fixture
def engine(tmp_path):
    store = MarketStore(tmp_path / "m.duckdb")
    bars = synth_bars(3000)
    store.upsert_candles(Market.CRYPTO_DELTA, "BTCUSD", "1h", bars)
    store.upsert_candles(Market.CRYPTO_DELTA, "FUNDING:BTCUSD", "1h", synth_funding(bars).assign(
        open=0.01, high=0.01, low=0.01, volume=0))
    cfg = AppConfig.model_validate({"strategies": {"min_confidence": 0.5, "min_net_rr": 1.0,
        "crypto": [{"name": "donchian_breakout", "symbols": ["BTCUSD"]}]}})
    return SignalEngine(cfg, store)


def test_engine_gates_and_signal_fields(engine):
    strat = engine.build(StrategyEntry(name="donchian_breakout", symbols=["BTCUSD"]))
    f = strat.prepare(engine.features("BTCUSD", "1h"))
    evs = [e for i in range(strat.warmup, len(f)) if (e := engine.evaluate(strat, f, i, "BTCUSD"))]
    assert evs, "synthetic data should trigger some breakouts"
    for e in evs:
        if e.regime in (Regime.HIGH_VOL, Regime.UNKNOWN):
            assert not e.passed
        if e.setup.side is Side.BUY and e.regime == Regime.TREND_DOWN:
            assert not e.passed
        if e.passed:
            s = e.signal
            assert s.stop_loss < s.entry < s.target if s.side is Side.BUY else s.target < s.entry < s.stop_loss
            assert s.max_hold == timedelta(hours=48) and s.reasoning and 0 <= s.confidence <= 1
            assert e.net_rr >= 1.0


def test_unknown_strategy_or_param_rejected(engine):
    with pytest.raises(ValueError):
        engine.build(StrategyEntry(name="nope"))
    with pytest.raises(ValueError):
        engine.build(StrategyEntry(name="ema_trend", params={"typo": 1}))
