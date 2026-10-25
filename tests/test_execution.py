import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from tradebot.audit import AuditLog
from tradebot.config import load_config
from tradebot.core.models import ProposalStatus
from tradebot.execution.engine import ExecutionEngine, ExecutionError
from tradebot.execution.exchange import (
    DeltaExchange,
    ExchangeError,
    PaperExchange,
    PositionInfo,
    round_to_tick,
)
from tradebot.execution.orders import (
    ExecutionStore,
    ManagedOrder,
    OrderKind,
    OrderState,
    OrderUpdate,
    PositionState,
)
from tradebot.markets.delta.client import DeltaClient
from tradebot.proposals.model import Proposal
from tradebot.risk.sizing import ContractSpec
from tradebot.risk.state import HaltKind, RiskStateStore

ROOT = Path(__file__).resolve().parents[1]
BTC = ContractSpec("BTCUSD", 27, 0.001, 0.5, 0.25, 125000)
T0 = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)


class FakeExchange:
    """Scriptable exchange: queue failures, control fills, inspect calls."""
    is_paper = True

    def __init__(self):
        self.calls, self.orders = [], {}
        self.pos = 0
        self.entry = None
        self.bracket = False
        self.leverage_reply = None
        self.fill_ratio = 1.0
        self.place_errors: list[Exception] = []      # raised before/after "reaching" exchange
        self.lost_after_send = False                  # True: order reached exchange, reply lost
        self.bracket_fails = 0
        self.price = 80_000.0
        self.exit_px = None

    def set_leverage(self, symbol, lev):
        self.calls.append(("leverage", lev))
        return self.leverage_reply if self.leverage_reply is not None else lev

    def place_order(self, o: ManagedOrder):
        self.calls.append(("place", o.kind.value, o.size, o.order_type, o.limit_price, o.reduce_only))
        filled = o.size if o.reduce_only else int(o.size * self.fill_ratio)
        u = OrderUpdate(o.client_id, "x" + o.client_id[-4:],
                        OrderState.FILLED if filled == o.size else OrderState.CANCELLED if not filled
                        else OrderState.PARTIAL, filled, self.price if filled else None)
        if self.place_errors:
            err = self.place_errors.pop(0)
            if self.lost_after_send:
                self._book(o, u)
            raise err
        self._book(o, u)
        return u

    def _book(self, o, u):
        self.orders[o.client_id] = u
        sgn = 1 if o.side == "buy" else -1
        self.pos += sgn * u.filled
        if not o.reduce_only and u.filled:
            self.entry = self.price
        if self.pos == 0:
            self.bracket = False
            self.exit_px = self.price

    def get_order(self, cid, symbol):
        self.calls.append(("get", cid))
        return self.orders.get(cid)

    def cancel_all(self, symbol):
        self.calls.append(("cancel_all",))
        self.bracket = False

    def place_bracket(self, symbol, side, stop, target):
        self.calls.append(("bracket", stop, target))
        if self.bracket_fails:
            self.bracket_fails -= 1
            raise ExchangeError("bracket failed")
        self.bracket = True

    def update_bracket(self, symbol, stop, target):
        self.calls.append(("update_bracket", stop, target))
        self.bracket = True

    def has_bracket(self, symbol):
        return self.bracket

    def position(self, symbol):
        return PositionInfo(self.pos, self.entry)

    def last_price(self, symbol):
        return self.price

    def last_exit_price(self, symbol):
        return self.exit_px


def proposal(contracts=10, side="buy", stop=79_000.0, target=82_000.0):
    return Proposal("p1", T0, T0, ProposalStatus.APPROVED, "crypto_delta", "BTCUSD", side,
                    "rsi_pullback", "swing", "1h", "trend_up", 80_000, stop, target, 0.7, "why",
                    contracts, contracts, 800, 1000, 1.0, 3, 0.7, 54_000, 72, "s1")


@pytest.fixture
def env(tmp_path):
    cfg, _ = load_config(ROOT / "config", env_file=None)
    x = FakeExchange()
    risk = RiskStateStore(tmp_path / "r.db", 100_000)
    msgs = []
    eng = ExecutionEngine(cfg, x, ExecutionStore(tmp_path / "e.db"), {"BTCUSD": BTC}, risk,
                          AuditLog(tmp_path / "logs", cfg.mode), msgs.append, sleep=lambda s: None)
    return eng, x, risk, msgs


def test_happy_path_sets_leverage_first_then_ioc_then_bracket(env):
    eng, x, _, _ = env
    eng.execute(proposal(), None, 80_000)
    kinds = [c[0] for c in x.calls]
    assert kinds[:3] == ["leverage", "place", "bracket"]
    _, kind, size, otype, limit, ro = x.calls[1]
    assert (kind, size, otype, ro) == ("entry", 10, "limit", False)
    assert limit == 80_080.0                       # +0.10% cap, rounded UP to 0.5 tick for a buy
    pos = eng.store.positions()[0]
    assert pos.bracket_ok and pos.contracts == 10 and pos.entry_price == 80_000


def test_wrong_leverage_aborts_before_any_order(env):
    eng, x, _, _ = env
    x.leverage_reply = 200.0
    with pytest.raises(ExecutionError, match="leverage"):
        eng.execute(proposal(), None, 80_000)
    assert not any(c[0] == "place" for c in x.calls)


def test_partial_fill_protects_only_filled_size(env):
    eng, x, _, msgs = env
    x.fill_ratio = 0.4
    eng.execute(proposal(), None, 80_000)
    assert eng.store.positions()[0].contracts == 4 and any("partial fill" in m for m in msgs)


def test_unfilled_ioc_raises_and_opens_nothing(env):
    eng, x, _, _ = env
    x.fill_ratio = 0
    with pytest.raises(ExecutionError, match="not filled"):
        eng.execute(proposal(), None, 80_000)
    assert not eng.store.positions()


def test_bracket_failure_closes_position_immediately(env):
    eng, x, _, msgs = env
    x.bracket_fails = 99
    with pytest.raises(ExecutionError, match="could NOT place exchange stop"):
        eng.execute(proposal(), None, 80_000)
    assert x.pos == 0                              # flattened
    assert eng.store.position("p1").state is PositionState.CLOSED
    assert any("ALERT" in m for m in msgs)


def test_lost_reply_is_adopted_not_resent(env):
    eng, x, _, _ = env
    x.place_errors = [httpx.ReadTimeout("timeout")]
    x.lost_after_send = True                       # order reached the exchange, reply didn't
    eng.execute(proposal(), None, 80_000)
    assert sum(1 for c in x.calls if c[0] == "place") == 1
    assert x.pos == 10                             # NOT 20


def test_retryable_error_before_send_is_retried(env):
    eng, x, _, _ = env
    x.place_errors = [ExchangeError("429", retryable=True)]
    eng.execute(proposal(), None, 80_000)
    assert sum(1 for c in x.calls if c[0] == "place") == 2 and x.pos == 10


def test_rejection_raises(env):
    eng, x, _, _ = env
    x.place_errors = [ExchangeError("insufficient_margin", "insufficient_margin")] * 5
    with pytest.raises(ExecutionError, match="rejected"):
        eng.execute(proposal(), None, 80_000)


def test_reconcile_detects_exchange_close_and_books_pnl(env):
    eng, x, risk, _ = env
    eng.execute(proposal(), None, 80_000)
    x.pos, x.bracket, x.exit_px = 0, False, 82_000.0          # target hit on exchange
    ev = eng.reconcile()
    assert ev == ["BTCUSD: closed on exchange (target)"]
    pos = eng.store.position("p1")
    assert pos.state is PositionState.CLOSED and pos.exit_reason == "target" and pos.pnl_usd > 0
    st = risk.state()
    assert st.equity_inr > 100_000 and st.recent_results[0][1] > 0


def test_reconcile_mismatch_orphan_and_missing_stop(env):
    eng, x, risk, _msgs = env
    eng.execute(proposal(), None, 80_000)
    x.bracket = False
    assert "re-placing" in eng.reconcile()[0] and x.bracket
    x.pos = 7
    assert "size mismatch" in eng.reconcile()[0]
    assert any(h.kind is HaltKind.RECONCILE for h in risk.active_halts())


def test_orphan_position_halts(env):
    eng, x, risk, _ = env
    x.pos = 3
    assert "ORPHAN" in eng.reconcile()[0]
    assert any(h.kind is HaltKind.RECONCILE for h in risk.active_halts())


def test_flatten_all_closes_with_reduce_only_market(env):
    eng, x, _, _ = env
    eng.execute(proposal(), None, 80_000)
    assert eng.flatten_all("kill switch") == ["BTCUSD: closed"]
    last_place = [c for c in x.calls if c[0] == "place"][-1]
    assert last_place[1:] == ("exit", 10, "market", None, True) and x.pos == 0


# ---------- live Delta adapter on a mocked HTTP transport ----------

def test_delta_exchange_requests():
    seen = []

    def handler(req: httpx.Request):
        body = json.loads(req.content) if req.content else None
        seen.append((req.method, req.url.path, body, dict(req.headers)))
        p = req.url.path
        if p.endswith("/orders/leverage"):
            return httpx.Response(200, json={"success": True, "result": {"leverage": "3", "product_id": 27}})
        if p == "/v2/orders" and req.method == "POST":
            if body["client_order_id"] == "boom":
                return httpx.Response(503, text="down")
            if body["client_order_id"] == "nomargin":
                return httpx.Response(400, json={"success": False, "error": {"code": "insufficient_margin"}})
            return httpx.Response(200, json={"success": True, "result": {
                "id": 9, "client_order_id": body["client_order_id"], "size": 10, "unfilled_size": 6,
                "state": "cancelled", "average_fill_price": "80010"}})
        if p == "/v2/orders/bracket":
            return httpx.Response(200, json={"success": True, "result": {}})
        if p == "/v2/positions":
            return httpx.Response(200, json={"success": True, "result": {"size": -4, "entry_price": "80010"}})
        return httpx.Response(404, text="?")

    c = DeltaClient("https://api.test", "key", "secret", transport=httpx.MockTransport(handler),
                    sleep=lambda s: None)
    x = DeltaExchange(c, {"BTCUSD": BTC})
    assert x.set_leverage("BTCUSD", 3) == 3.0
    o = ManagedOrder("p1", "BTCUSD", 27, OrderKind.ENTRY, "buy", 10, "limit", 80_080.0)
    u = x.place_order(o)
    assert (u.state, u.filled, u.avg_price) == (OrderState.CANCELLED, 4, 80_010.0)  # IOC remainder cancelled
    _m, _path, body, headers = seen[1]
    assert body == {"product_id": 27, "size": 10, "side": "buy", "order_type": "limit_order",
                    "reduce_only": False, "client_order_id": o.client_id, "limit_price": "80080.0",
                    "time_in_force": "ioc"}
    assert headers["api-key"] == "key" and len(headers["signature"]) == 64
    with pytest.raises(ExchangeError) as ei:
        x.place_order(ManagedOrder("p1", "BTCUSD", 27, OrderKind.ENTRY, "buy", 1, "market", client_id="boom"))
    assert ei.value.retryable and sum(1 for s in seen if s[2] and s[2].get("client_order_id") == "boom") == 1
    rej = x.place_order(ManagedOrder("p1", "BTCUSD", 27, OrderKind.ENTRY, "buy", 1, "market", client_id="nomargin"))
    assert rej.state is OrderState.REJECTED and rej.error == "insufficient_margin"
    x.place_bracket("BTCUSD", "buy", 79_000, 82_000)
    br = next(s for s in seen if s[1] == "/v2/orders/bracket")[2]
    assert br["stop_loss_order"] == {"order_type": "market_order", "stop_price": "79000"}
    assert br["take_profit_order"]["limit_price"] == "82000"
    assert x.position("BTCUSD") == PositionInfo(-4, 80_010.0)


def test_round_to_tick():
    assert round_to_tick(80_080.2, 0.5, up=True) == 80_080.5
    assert round_to_tick(80_080.2, 0.5, up=False) == 80_080.0


# ---------- paper exchange ----------

class FakeTickerClient:
    def __init__(self):
        self.bid, self.ask, self.last = 79_990.0, 80_010.0, 80_000.0

    def ticker(self, sym):
        return {"quotes": {"best_bid": self.bid, "best_ask": self.ask}, "close": self.last}


def test_paper_exchange_fills_at_touch_and_triggers_brackets(tmp_path):
    c = FakeTickerClient()
    x = PaperExchange(c, {"BTCUSD": BTC}, tmp_path / "paper.json")
    o = ManagedOrder("p1", "BTCUSD", 27, OrderKind.ENTRY, "buy", 5, "limit", 80_050.0)
    u = x.place_order(o)
    assert (u.state, u.filled, u.avg_price) == (OrderState.FILLED, 5, 80_010.0)
    assert x.place_order(ManagedOrder("p2", "BTCUSD", 27, OrderKind.ENTRY, "buy", 5, "limit",
                                      80_000.0)).state is OrderState.CANCELLED   # limit below ask
    x.place_bracket("BTCUSD", "buy", 79_000, 82_000)
    x2 = PaperExchange(c, {"BTCUSD": BTC}, tmp_path / "paper.json")           # survives restart
    assert x2.position("BTCUSD").size == 5 and x2.has_bracket("BTCUSD")
    c.last = 78_900
    assert x2.poll() == ["BTCUSD"] and x2.position("BTCUSD").size == 0
    assert x2.last_exit_price("BTCUSD") == 78_900
