from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tradebot.audit import AuditLog
from tradebot.config import load_config
from tradebot.core.models import Horizon, Instrument, Market, ProposalStatus, Side, Signal
from tradebot.proposals.engine import PaperLogExecutor, ProposalEngine
from tradebot.proposals.model import render
from tradebot.proposals.store import ProposalStore
from tradebot.risk.engine import CryptoRiskEngine, MarketContext
from tradebot.risk.events import EventCalendar
from tradebot.risk.manager import RiskManager
from tradebot.risk.sizing import ContractSpec
from tradebot.risk.state import RiskStateStore
from tradebot.timing.clock import TradingClock

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)       # Fri 11:30 IST
BTC = ContractSpec("BTCUSD", 27, 0.001, 0.5, 0.25, 125000)
OWNER = "123456789"


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class Env:
    def __init__(self, tmp_path, **cfg_over):
        cfg, _ = load_config(ROOT / "config", env_file=None)
        cfg = cfg.model_copy(update={"telegram": cfg.telegram.model_copy(
            update={"allowed_user_ids": [int(OWNER)]}), **cfg_over})
        self.cfg = cfg
        self.clock = Clock()
        self.px = {"BTCUSD": 80_000.0}
        self.audit = AuditLog(tmp_path / "logs", cfg.mode)
        risk = RiskManager(CryptoRiskEngine(cfg, {"BTCUSD": BTC}, TradingClock(cfg), EventCalendar([])),
                           RiskStateStore(tmp_path / "r.db", cfg.account.capital_inr), self.audit)
        self.store = ProposalStore(tmp_path / "p.db")
        self.eng = ProposalEngine(cfg, risk, self.store, lambda s: self.px[s],
                                  PaperLogExecutor(self.audit), self.audit, None, self.clock)

    def events(self):
        import json
        return [json.loads(line)["event"] for f in sorted((self.audit.dir).glob("*.jsonl"))
                for line in f.read_text().splitlines()]


def sig(horizon=Horizon.SWING, entry=80_000.0, stop=79_000.0, target=82_000.0, side=Side.BUY):
    return Signal(Instrument(Market.CRYPTO_DELTA, "BTCUSD"), side, "rsi_pullback", horizon,
                  Decimal(str(entry)), Decimal(str(stop)), Decimal(str(target)), 0.7,
                  "uptrend, RSI recovered", T0, "1h", "trend_up", timedelta(hours=72))


CTX = MarketContext(T0, None, T0)


def test_create_has_all_fields_and_renders(tmp_path):
    env = Env(tmp_path)
    out = env.eng.create(sig(), CTX)
    p = out.proposal
    assert out.ok and p.status is ProposalStatus.PENDING
    assert p.expires_at == T0 + timedelta(minutes=60) and p.contracts == 10 and p.risk_pct <= 1.0
    text = render(p, T0)
    for needle in ("BTCUSD LONG", "rsi_pullback (swing, 1h)", "Entry 80,000.00", "SL 79,000.00",
                   "R:R 2.00", "10 contracts", "Capital at risk ₹", "Leverage: order 3x",
                   "Confidence 0.70", "Why: uptrend", "Expires", "60m 0s left"):
        assert needle in text, needle
    assert env.events()[-1] == "proposal_created"


def test_expiry_rules(tmp_path):
    env = Env(tmp_path)
    assert env.eng.expiry_for(Horizon.INTRADAY, T0) == T0 + timedelta(minutes=5)
    assert env.eng.expiry_for(Horizon.LONG_TERM, T0) == datetime(2026, 10, 16, 18, 30, tzinfo=UTC)


def test_approve_executes_and_is_idempotent(tmp_path):
    env = Env(tmp_path)
    p = env.eng.create(sig(), CTX).proposal
    env.clock.t += timedelta(minutes=2)
    env.px["BTCUSD"] = 80_100.0                      # +0.125%: within 0.3%
    out = env.eng.approve(p.id, OWNER)
    assert out.ok and out.proposal.status is ProposalStatus.EXECUTED
    assert out.proposal.decided_by == OWNER and out.proposal.entry == 80_100
    again = env.eng.approve(p.id, OWNER)
    assert not again.ok and "already executed" in again.message
    ev = env.events()
    assert ev.index("proposal_approved") < ev.index("order_submitted")


def test_unauthorized_user_ignored_and_logged(tmp_path):
    env = Env(tmp_path)
    p = env.eng.create(sig(), CTX).proposal
    out = env.eng.approve(p.id, "999")
    assert not out.ok and out.message == "not authorized"
    assert env.store.get(p.id)[0].status is ProposalStatus.PENDING
    assert "unauthorized_sender" in env.events()


def test_expired_never_executes(tmp_path):
    env = Env(tmp_path)
    p = env.eng.create(sig(Horizon.INTRADAY), CTX).proposal
    env.clock.t += timedelta(minutes=5, seconds=1)
    out = env.eng.approve(p.id, OWNER)
    assert not out.ok and "expired" in out.message
    assert env.store.get(p.id)[0].status is ProposalStatus.EXPIRED
    assert "order_submitted" not in env.events()


def test_sweeper_expires(tmp_path):
    env = Env(tmp_path)
    env.eng.create(sig(Horizon.INTRADAY), CTX)
    env.clock.t += timedelta(minutes=6)
    assert len(env.eng.expire_due()) == 1 and not env.store.pending()


def test_drift_cancels_and_sends_fresh_proposal(tmp_path):
    env = Env(tmp_path)
    p = env.eng.create(sig(), CTX).proposal
    env.px["BTCUSD"] = 79_500.0                      # -0.625%: beyond 0.3%
    out = env.eng.approve(p.id, OWNER)
    assert not out.ok and env.store.get(p.id)[0].status is ProposalStatus.DRIFT_CANCELLED
    fresh = out.fresh
    assert fresh and fresh.supersedes == p.id and fresh.entry == 79_500 and fresh.status is ProposalStatus.PENDING
    assert fresh.rr == pytest.approx(2500 / 500)
    env.px["BTCUSD"] = 81_900.0                      # near target: R:R collapses -> no refresh
    out2 = env.eng.approve(fresh.id, OWNER)
    assert out2.fresh is None and "no longer valid" in out2.message


def test_risk_engine_blocks_even_after_approval(tmp_path):
    env = Env(tmp_path)
    p = env.eng.create(sig(), CTX).proposal
    env.eng.risk.kill_switch("test", by="pytest")    # e.g. you hit /killswitch after approving
    out = env.eng.approve(p.id, OWNER)
    assert not out.ok and out.proposal.status is ProposalStatus.RISK_BLOCKED
    assert any("kill_switch" in r for r in out.reasons)


def test_modify_size_and_stop_revalidates(tmp_path):
    env = Env(tmp_path)
    p = env.eng.create(sig(), CTX).proposal
    out = env.eng.modify(p.id, OWNER, contracts=50)
    assert out.ok and out.proposal.contracts == 10 and "capped" in out.message
    out = env.eng.modify(p.id, OWNER, contracts=4)
    assert out.proposal.contracts == 4 and out.proposal.version == 3
    out = env.eng.modify(p.id, OWNER, stop=79_500)    # tighter stop -> more allowed, keep 4
    assert out.proposal.stop == 79_500 and out.proposal.max_contracts > 10 and out.proposal.contracts == 4
    bad = env.eng.modify(p.id, OWNER, stop=80_500)    # stop above entry for a long
    assert not bad.ok and env.store.get(p.id)[0].stop == 79_500
    assert env.eng.approve(p.id, OWNER).proposal.contracts == 4   # executes the modified size


def test_duplicate_and_blocked_create(tmp_path):
    env = Env(tmp_path)
    env.eng.create(sig(), CTX)
    assert "duplicate" in env.eng.create(sig(), CTX).message
    blocked = env.eng.create(sig(side=Side.SELL, stop=81_000, target=78_000),
                             MarketContext(T0, None, T0 - timedelta(hours=5)))
    assert not blocked.ok and any("stale data" in r for r in blocked.reasons)


def test_auto_approve_only_when_enabled_and_small(tmp_path):
    env = Env(tmp_path)
    env.cfg.approval.auto_approve.enabled = True
    env.cfg.approval.auto_approve.max_risk_inr = 100   # proposal risks ~₹1000 -> not auto
    assert env.eng.create(sig(), CTX).proposal.status is ProposalStatus.PENDING
    env2 = Env(tmp_path / "b")
    env2.cfg.approval.auto_approve.enabled = True
    env2.cfg.approval.auto_approve.max_risk_inr = 5000
    out = env2.eng.create(sig(), CTX)
    assert out.proposal.status is ProposalStatus.EXECUTED and out.proposal.decided_by == "auto"


def test_restart_keeps_pending_and_expiry(tmp_path):
    env = Env(tmp_path)
    p = env.eng.create(sig(), CTX).proposal
    env.store.close()
    store = ProposalStore(tmp_path / "p.db")
    q, s = store.get(p.id)
    assert q.status is ProposalStatus.PENDING and q.expires_at == p.expires_at
    assert s.entry == Decimal("80000.0") and s.max_hold == timedelta(hours=72)
