from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from tradebot.config import load_config
from tradebot.core.models import Horizon, Instrument, Market, ProposalStatus, Side, Signal
from tradebot.data.store import MarketStore
from tradebot.execution.orders import ExecutionStore
from tradebot.paper.evaluate import evaluate_outcomes
from tradebot.paper.ledger import ForwardLedger
from tradebot.paper.report import build_report
from tradebot.proposals.model import Proposal
from tradebot.proposals.store import ProposalStore
from tradebot.risk.state import RiskStateStore

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 3, 2, 6, 0, tzinfo=UTC)


@pytest.fixture
def cfg():
    cfg, _ = load_config(ROOT / "config", env_file=None)
    return cfg


def sig(sid, side=Side.BUY, strategy="rsi_pullback"):
    return Signal(Instrument(Market.CRYPTO_DELTA, "BTCUSD"), side, strategy, Horizon.SWING,
                  Decimal(100), Decimal(98), Decimal(104), 0.7, "r", T0, "1h", "trend_up",
                  timedelta(hours=2), {"atr_pct": 0.01}, sid)


def five_min(store, closes):
    ts = pd.date_range(T0, periods=len(closes), freq="5min", tz="UTC")
    df = pd.DataFrame({"ts": ts, "open": closes, "high": [c + 0.2 for c in closes],
                       "low": [c - 0.2 for c in closes], "close": closes, "volume": 1.0})
    store.upsert_candles(Market.CRYPTO_DELTA, "BTCUSD", "5m", df)


def test_ledger_records_once_and_waits_for_max_hold(tmp_path):
    led = ForwardLedger(tmp_path / "f.db")
    led.record(sig("s1"), T0, enabled=True)
    led.record(sig("s1"), T0, enabled=True)
    led.record(sig("s2", strategy="ema_trend"), T0, enabled=False)
    assert len(led.frame()) == 2
    assert led.awaiting_outcome(T0 + timedelta(hours=2)) == []          # max hold 2h + 1h buffer
    assert len(led.awaiting_outcome(T0 + timedelta(hours=3))) == 2


def test_uptime(tmp_path):
    led = ForwardLedger(tmp_path / "f.db")
    for m in range(0, 60, 5):
        if m != 30:
            led.beat(T0 + timedelta(minutes=m, seconds=7))
    assert led.uptime_pct(T0, T0 + timedelta(hours=1)) == pytest.approx(100 * 11 / 12)


def test_outcomes_use_backtest_execution_model(tmp_path, cfg):
    store = MarketStore(tmp_path / "m.duckdb")
    five_min(store, [100] * 2 + [101, 102, 103, 104.5] + [104] * 40)    # long hits target
    led = ForwardLedger(tmp_path / "f.db")
    led.record(sig("win"), T0, True)
    led.record(sig("short", side=Side.SELL), T0, True)                  # stop 98 > entry: invalid
    assert evaluate_outcomes(cfg, led, store, T0 + timedelta(hours=4)) == 2
    f = led.frame().set_index("signal_id")
    assert f.loc["win", "exit_reason"] == "target" and 2.0 < f.loc["win", "net_r"] < 2.3   # gapped through: filled at open
    assert f.loc["short", "sim_status"] in ("filled", "skipped")


def prop(pid, status, risk_inr=1000.0, created=T0, decided_by="111"):
    return Proposal(pid, created, created + timedelta(hours=1), status, "crypto_delta", "BTCUSD", "buy",
                    "rsi_pullback", "swing", "1h", "trend_up", 100, 98, 104, 0.7, "r", 5, 5, 500,
                    risk_inr, 1.0, 3, 0.4, 70, 2, "s", decided_at=created + timedelta(minutes=4),
                    decided_by=decided_by)


def test_report_values_rejections_and_gates_readiness(tmp_path, cfg):
    led = ForwardLedger(tmp_path / "f.db")
    led.db.execute("INSERT INTO meta VALUES ('paper_started', ?)", ((T0 - timedelta(days=1)).isoformat(),))
    ps = ProposalStore(tmp_path / "p.db")
    for pid, status, r in (("rej", ProposalStatus.REJECTED, -1.0), ("exp", ProposalStatus.EXPIRED, 2.0)):
        s = sig(f"s-{pid}")
        ps.save(prop(pid, status, decided_by="111" if pid == "rej" else "system"), s)
        led.record(s, T0, True)
        led.link(s.id, pid, status.value)
        led.set_outcome(s.id, "filled", "x", r)
    text, checks = build_report(cfg, led, ps, ExecutionStore(tmp_path / "e.db"),
                                RiskStateStore(tmp_path / "r.db", 100_000), now=T0 + timedelta(days=2))
    assert "would have made ₹+1,000" in text and "COST you money" in text
    by = {c.name: c for c in checks}
    assert not by["duration"].ok and not by["forward sample size"].ok
    assert not by["you answer proposals in time"].ok            # 1 of 2 expired = 50%
    assert "NOT READY" in text


def test_report_ready_when_everything_passes(tmp_path, cfg):
    cfg = cfg.model_copy(deep=True)
    cfg.paper.min_weeks, cfg.paper.min_forward_trades = 1, 5
    led = ForwardLedger(tmp_path / "f.db")
    start = T0 - timedelta(days=8)
    led.db.execute("INSERT INTO meta VALUES ('paper_started', ?)", (start.isoformat(),))
    t = start
    while t < T0:
        led.beat(t)
        t += timedelta(minutes=5)
    for i in range(6):
        led.record(sig(f"f{i}"), T0, True)
        led.set_outcome(f"f{i}", "filled", "target", 0.5)
    text, checks = build_report(cfg, led, ProposalStore(tmp_path / "p.db"), ExecutionStore(tmp_path / "e.db"),
                                RiskStateStore(tmp_path / "r.db", 100_000), now=T0)
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    assert "READY ✅" in text
