"""Exchange adapters behind one interface: live Delta (signed REST) and a paper simulator.

The execution engine only talks to `Exchange`, so paper and live run the exact same logic.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from tradebot.data.http import HttpError
from tradebot.execution.orders import ManagedOrder, OrderState, OrderUpdate
from tradebot.markets.delta.client import DeltaClient
from tradebot.risk.sizing import ContractSpec
from tradebot.timeutil import now_utc


@dataclass
class PositionInfo:
    size: int              # signed contracts (+ long)
    entry_price: float | None


class ExchangeError(RuntimeError):
    def __init__(self, msg: str, code: str | None = None, retryable: bool = False):
        self.code, self.retryable = code, retryable
        super().__init__(msg)


class Exchange(Protocol):
    is_paper: bool

    def set_leverage(self, symbol: str, leverage: float) -> float: ...
    def place_order(self, o: ManagedOrder) -> OrderUpdate: ...
    def get_order(self, client_id: str, symbol: str) -> OrderUpdate | None: ...
    def cancel_all(self, symbol: str) -> None: ...
    def place_bracket(self, symbol: str, side: str, stop: float, target: float | None) -> None: ...
    def update_bracket(self, symbol: str, stop: float, target: float | None) -> None: ...
    def has_bracket(self, symbol: str) -> bool: ...
    def position(self, symbol: str) -> PositionInfo: ...
    def last_price(self, symbol: str) -> float: ...
    def last_exit_price(self, symbol: str) -> float | None: ...


def round_to_tick(px: float, tick: float, up: bool) -> float:
    n = px / tick
    return round((math.ceil(n) if up else math.floor(n)) * tick, 10)


_STATE = {"open": OrderState.OPEN, "pending": OrderState.OPEN, "closed": OrderState.FILLED,
          "cancelled": OrderState.CANCELLED}


def _update(r: dict) -> OrderUpdate:
    size, unfilled = int(r.get("size") or 0), int(r.get("unfilled_size") or 0)
    filled = size - unfilled
    state = _STATE.get(r.get("state", ""), OrderState.OPEN)
    if state is OrderState.FILLED and unfilled:
        state = OrderState.PARTIAL
    if state is OrderState.OPEN and filled:
        state = OrderState.PARTIAL
    avg = r.get("average_fill_price")
    return OrderUpdate(r.get("client_order_id", ""), str(r.get("id")), state, filled,
                       float(avg) if avg not in (None, "") else None)


class DeltaExchange:
    """Live Delta Exchange India. Only constructed after validate_live() (see factory)."""

    is_paper = False

    def __init__(self, client: DeltaClient, specs: dict[str, ContractSpec],
                 stop_trigger: str = "last_traded_price"):
        self.c, self.specs, self.stop_trigger = client, specs, stop_trigger

    def _pid(self, symbol: str) -> int:
        return self.specs[symbol].product_id

    def set_leverage(self, symbol: str, leverage: float) -> float:
        r = self.c._call("POST", f"/v2/products/{self._pid(symbol)}/orders/leverage",
                         body={"leverage": f"{leverage:g}"}, auth=True)
        return float(r["leverage"])

    def place_order(self, o: ManagedOrder) -> OrderUpdate:
        body = {"product_id": o.product_id, "size": o.size, "side": o.side,
                "order_type": "limit_order" if o.order_type == "limit" else "market_order",
                "reduce_only": o.reduce_only, "client_order_id": o.client_id}
        if o.order_type == "limit":
            body |= {"limit_price": f"{o.limit_price}", "time_in_force": o.time_in_force}
        try:
            r = self.c._call("POST", "/v2/orders", body=body, auth=True, retries=0)
        except HttpError as e:
            if e.status == 429 or e.status >= 500:
                raise ExchangeError(str(e), e.code, retryable=True) from e
            return OrderUpdate(o.client_id, None, OrderState.REJECTED, 0, None, e.code or e.body[:200])
        return _update(r)

    def get_order(self, client_id: str, symbol: str) -> OrderUpdate | None:
        try:
            return _update(self.c._call("GET", f"/v2/orders/client_order_id/{client_id}", auth=True))
        except HttpError as e:
            if e.status == 404:
                return None
            raise

    def cancel_all(self, symbol: str) -> None:
        self.c._call("DELETE", "/v2/orders/all", body={"product_id": self._pid(symbol)}, auth=True)

    def place_bracket(self, symbol: str, side: str, stop: float, target: float | None) -> None:
        """Position-level SL (+ optional TP) held ON the exchange: protects the position even if
        the bot dies. target=None when exits.exit_on_target is "ask"."""
        body = {"product_id": self._pid(symbol),
                "stop_loss_order": {"order_type": "market_order", "stop_price": f"{stop}"},
                "bracket_stop_trigger_method": self.stop_trigger}
        if target is not None:
            body["take_profit_order"] = {"order_type": "limit_order", "stop_price": f"{target}",
                                         "limit_price": f"{target}"}
        self.c._call("POST", "/v2/orders/bracket", auth=True, retries=0, body=body)

    def update_bracket(self, symbol: str, stop: float, target: float | None) -> None:
        """Move the exchange stop in place (trailing / breakeven). VERIFY ON TESTNET before live."""
        body = {"product_id": self._pid(symbol), "bracket_stop_loss_price": f"{stop}",
                "bracket_stop_trigger_method": self.stop_trigger}
        if target is not None:
            body |= {"bracket_take_profit_price": f"{target}", "bracket_take_profit_limit_price": f"{target}"}
        self.c._call("PUT", "/v2/orders/bracket", auth=True, retries=0, body=body)

    def has_bracket(self, symbol: str) -> bool:
        rows = self.c._call("GET", "/v2/orders", params={
            "product_ids": self._pid(symbol), "states": "open,pending"}, auth=True)
        return any(r.get("stop_order_type") == "stop_loss_order" or r.get("bracket_order")
                   for r in rows)

    def position(self, symbol: str) -> PositionInfo:
        r = self.c._call("GET", "/v2/positions", params={"product_id": self._pid(symbol)}, auth=True)
        size = int(r.get("size") or 0)
        ep = r.get("entry_price")
        return PositionInfo(size, float(ep) if ep else None)

    def last_price(self, symbol: str) -> float:
        return float(self.c.last_price(symbol))

    def last_exit_price(self, symbol: str) -> float | None:
        fills = self.c._call("GET", "/v2/fills", params={"product_ids": self._pid(symbol),
                                                         "page_size": 5}, auth=True)
        return float(fills[0]["price"]) if fills else None


@dataclass
class _PaperPos:
    size: int = 0
    entry: float = 0.0
    stop: float | None = None
    target: float | None = None
    last_exit: float | None = None


class PaperExchange:
    """Simulated exchange on LIVE prices. Marketable IOC limits fill at best bid/ask (if within the
    limit), bracket stop/target trigger when `poll()` sees the last price cross them. State is a
    JSON file so separate CLI runs see the same paper account."""

    is_paper = True

    def __init__(self, client: DeltaClient, specs: dict[str, ContractSpec], state_file: Path | str):
        self.c, self.specs, self.path = client, specs, Path(state_file)
        self.orders: dict[str, dict] = {}
        self.pos: dict[str, _PaperPos] = {}
        self.leverage: dict[str, float] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self.orders, self.leverage = raw["orders"], raw["leverage"]
            self.pos = {k: _PaperPos(**v) for k, v in raw["pos"].items()}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"orders": self.orders, "leverage": self.leverage,
                                         "pos": {k: asdict(v) for k, v in self.pos.items()}}))

    def _book(self, symbol: str) -> tuple[float, float, float]:
        t = self.c.ticker(symbol)
        q = t["quotes"]
        return float(q["best_bid"]), float(q["best_ask"]), float(t["close"])

    def set_leverage(self, symbol: str, leverage: float) -> float:
        self.leverage[symbol] = leverage
        self._save()
        return leverage

    def place_order(self, o: ManagedOrder) -> OrderUpdate:
        bid, ask, _ = self._book(o.symbol)
        px = ask if o.side == "buy" else bid
        p = self.pos.setdefault(o.symbol, _PaperPos())
        crosses = o.order_type == "market" or (px <= o.limit_price if o.side == "buy" else px >= o.limit_price)
        size = o.size
        if o.reduce_only:
            size = min(size, abs(p.size))
        if not crosses or size <= 0:
            u = OrderUpdate(o.client_id, f"paper-{o.client_id[-8:]}", OrderState.CANCELLED, 0, None)
        else:
            sgn = 1 if o.side == "buy" else -1
            new = p.size + sgn * size
            if p.size == 0 or (p.size > 0) == (sgn > 0):
                p.entry = (p.entry * abs(p.size) + px * size) / abs(new)
            if new == 0:
                p.stop = p.target = None
                p.last_exit = px
            p.size = new
            u = OrderUpdate(o.client_id, f"paper-{o.client_id[-8:]}", OrderState.FILLED, size, px)
        self.orders[o.client_id] = asdict(u) | {"state": u.state.value}
        self._save()
        return u

    def get_order(self, client_id: str, symbol: str) -> OrderUpdate | None:
        r = self.orders.get(client_id)
        return OrderUpdate(**(r | {"state": OrderState(r["state"])})) if r else None

    def cancel_all(self, symbol: str) -> None:
        p = self.pos.get(symbol)
        if p:
            p.stop = p.target = None
        self._save()

    def place_bracket(self, symbol: str, side: str, stop: float, target: float | None) -> None:
        p = self.pos.get(symbol)
        if not p or p.size == 0:
            raise ExchangeError("no position to attach bracket to", "no_position")
        p.stop, p.target = stop, target
        self._save()

    def update_bracket(self, symbol: str, stop: float, target: float | None) -> None:
        self.place_bracket(symbol, "", stop, target)

    def has_bracket(self, symbol: str) -> bool:
        p = self.pos.get(symbol)
        return bool(p and p.stop is not None)

    def position(self, symbol: str) -> PositionInfo:
        p = self.pos.get(symbol, _PaperPos())
        return PositionInfo(p.size, p.entry or None)

    def last_price(self, symbol: str) -> float:
        return self._book(symbol)[2]

    def last_exit_price(self, symbol: str) -> float | None:
        p = self.pos.get(symbol)
        return p.last_exit if p else None

    def poll(self) -> list[str]:
        """Trigger paper brackets against the live last price. Returns symbols that closed."""
        closed = []
        for sym, p in self.pos.items():
            if p.size == 0 or p.stop is None:
                continue
            last = self._book(sym)[2]
            long_ = p.size > 0
            hit_stop = last <= p.stop if long_ else last >= p.stop
            hit_tgt = p.target is not None and (last >= p.target if long_ else last <= p.target)
            if hit_stop or hit_tgt:
                p.last_exit = last if hit_stop else p.target
                p.size, p.stop, p.target = 0, None, None
                closed.append(sym)
        self._save()
        return closed


def now_iso() -> str:
    return now_utc().isoformat()
