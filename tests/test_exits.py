from datetime import timedelta

import pandas as pd
import pytest
from test_execution import BTC, ROOT, T0, FakeExchange, proposal

from tradebot.audit import AuditLog
from tradebot.config import load_config
from tradebot.execution.engine import ExecutionEngine
from tradebot.execution.exits import PositionManager
from tradebot.execution.orders import ExecutionStore, PositionState
from tradebot.risk.state import RiskStateStore

OWNER = "123456789"


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def make(tmp_path, exit_on_target="auto", trailing=False, features=None):
    cfg, _ = load_config(ROOT / "config", env_file=None)
    cfg = cfg.model_copy(deep=True)
    cfg.telegram.allowed_user_ids = [int(OWNER)]
    cfg.approval.exit_on_target = exit_on_target
    cfg.exits.trailing_enabled = trailing
    clock, x, msgs = Clock(), FakeExchange(), []
    eng = ExecutionEngine(cfg, x, ExecutionStore(tmp_path / "e.db"), {"BTCUSD": BTC},
                          RiskStateStore(tmp_path / "r.db", 100_000),
                          AuditLog(tmp_path / "logs", cfg.mode), msgs.append, sleep=lambda s: None,
                          clock=clock)
    pm = PositionManager(cfg, eng, msgs.append, clock, features=features)
    return pm, eng, x, clock, msgs


class Sig:
    def __init__(self):
        self.features = {"atr_pct": 0.006}


def open_long(eng, x):
    x.price = 80_000.0
    eng.execute(proposal(), Sig(), 80_000)        # stop 79,000 (1R = 1000), target 82,000, 72h
    return eng.store.positions()[0]


def test_max_hold_closes_small_winners_and_losers(tmp_path):
    pm, eng, x, clock, _ = make(tmp_path)
    open_long(eng, x)
    x.price = 80_400                              # +0.4R
    clock.t += timedelta(hours=72, minutes=1)
    assert "BTCUSD: closed at max hold" in pm.tick()
    assert eng.store.position("p1").exit_reason.startswith("max hold time")


def test_extension_needs_approval_and_is_limited(tmp_path):
    pm, eng, x, clock, _msgs = make(tmp_path)
    open_long(eng, x)
    x.price = 81_500                              # +1.5R
    clock.t += timedelta(hours=72, minutes=1)
    assert "asked to extend" in pm.tick()[0]
    req = eng.store.requests()[0]
    assert pm.answer(req.id, "hold", "999") == "not authorized"
    assert "held until" in pm.answer(req.id, "hold", OWNER)
    pos = eng.store.positions()[0]
    assert pos.extensions == 1 and pos.state is PositionState.OPEN
    clock.t += timedelta(hours=48, minutes=1)     # second max-hold: extensions used up
    assert "closed at max hold" in " ".join(pm.tick())


def test_unanswered_extension_defaults_to_close(tmp_path):
    pm, eng, x, clock, _ = make(tmp_path)
    open_long(eng, x)
    x.price = 81_500
    clock.t += timedelta(hours=72, minutes=1)
    pm.tick()
    clock.t += timedelta(minutes=31)
    ev = pm.tick()
    assert any("timed out -> close" in e for e in ev)
    assert eng.store.position("p1").state is PositionState.CLOSED


def test_trailing_ratchets_and_never_loosens(tmp_path):
    pm, eng, x, _clock, _ = make(tmp_path, trailing=True)
    pos = open_long(eng, x)
    assert pos.trail_dist == pytest.approx(2.0 * 0.006 * 80_000)     # 960
    x.price = 80_500                              # +0.5R: not active
    pm.tick()
    assert eng.store.positions()[0].stop == 79_000
    x.price = 81_200                              # +1.2R -> stop to 80,240
    pm.tick()
    assert eng.store.positions()[0].stop == 80_240
    x.price = 80_900                              # pullback: stop must NOT move down
    pm.tick()
    assert eng.store.positions()[0].stop == 80_240
    assert ("update_bracket", 80_240.0, 82_000.0) in x.calls


def test_target_ask_mode(tmp_path):
    pm, eng, x, _clock, _msgs = make(tmp_path, exit_on_target="ask")
    open_long(eng, x)
    assert ("bracket", 79_000.0, None) in x.calls             # no TP resting on the exchange
    x.price = 82_100
    assert pm.tick() == ["BTCUSD: target reached, asked"]
    pos = eng.store.positions()[0]
    assert pos.stop == 81_000 and pos.target_prompted        # locked half the move (+1R)
    req = eng.store.requests()[0]
    assert pm.answer(req.id, "close", OWNER) == "BTCUSD closed"
    assert eng.store.position("p1").state is PositionState.CLOSED


def test_daily_review_alerts_but_never_closes(tmp_path):
    feats = pd.DataFrame({"atr": [500.0], "regime": ["trend_down"], "funding": [0.05]})
    pm, eng, x, _clock, _ = make(tmp_path, features=lambda s: feats)
    open_long(eng, x)
    x.price = 79_100
    text = pm.daily_review()
    assert "REVIEW: consider closing" in text and "regime flipped to trend_down" in text
    assert "funding costs 0.150%/day" in text and "0.2 ATR from stop" in text
    assert eng.store.positions()[0].state is PositionState.OPEN
