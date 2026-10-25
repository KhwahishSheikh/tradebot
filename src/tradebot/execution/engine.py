"""Execution engine (implements the proposal engine's Executor).

Entry sequence for an approved proposal:
  1. set leverage explicitly and verify it (Delta defaults to 200x)
  2. entry = IOC limit at live price +/- max_entry_slippage (fills what it can, rest cancels;
     a partial fill just means a smaller position, the stop covers whatever filled)
  3. position-level bracket (stop-loss + take-profit) placed ON the exchange and verified
  4. if the bracket cannot be placed after retries: close the position at market immediately
Order placement is idempotent: on a timeout/5xx we look the order up by client_order_id before
ever sending it again, so a network blip can't double a position.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import httpx

from tradebot.audit import AuditEvent, AuditLog
from tradebot.config import AppConfig
from tradebot.core.models import Signal
from tradebot.execution.exchange import Exchange, ExchangeError, round_to_tick
from tradebot.execution.orders import (
    ExecutionStore,
    ManagedOrder,
    ManagedPosition,
    OrderKind,
    OrderState,
    PositionState,
)
from tradebot.proposals.model import Proposal
from tradebot.risk.sizing import ContractSpec
from tradebot.risk.state import HaltKind, OpenRisk, RiskStateStore
from tradebot.timeutil import now_utc

log = logging.getLogger(__name__)


class ExecutionError(RuntimeError):
    pass


class ExecutionEngine:
    def __init__(self, cfg: AppConfig, exchange: Exchange, store: ExecutionStore,
                 specs: dict[str, ContractSpec], risk_store: RiskStateStore,
                 audit: AuditLog | None, notify=print, sleep=time.sleep, clock=now_utc):
        self.cfg, self.x, self.store, self.specs = cfg, exchange, store, specs
        self.risk_store, self.audit, self.notify = risk_store, audit, notify
        self.sleep, self.now = sleep, clock
        self.ec = cfg.execution

    # ---------- helpers ----------

    def _audit(self, event: AuditEvent, **data) -> None:
        if self.audit:
            self.audit.record(event, paper=self.x.is_paper, **data)

    def open_risk(self) -> list[OpenRisk]:
        """Open positions in the form the risk engine needs."""
        return [OpenRisk(p.symbol, p.side, p.contracts, p.entry_price, p.stop, p.contract_value)
                for p in self.store.positions()]

    def _exchange_target(self, pos: ManagedPosition) -> float | None:
        """TP rests on the exchange only when exits on target are automatic."""
        return pos.target if self.cfg.approval.exit_on_target == "auto" else None

    def move_stop(self, pos: ManagedPosition, new_stop: float, why: str) -> bool:
        """Tighten the exchange stop (never loosens). Falls back to cancel + re-place."""
        long_ = pos.side == "buy"
        if (long_ and new_stop <= pos.stop) or (not long_ and new_stop >= pos.stop):
            return False
        new_stop = round_to_tick(new_stop, self.specs[pos.symbol].tick_size, up=long_)
        old = pos.stop
        try:
            self.x.update_bracket(pos.symbol, new_stop, self._exchange_target(pos))
        except Exception as e:  # noqa: BLE001 - fall back to a fresh bracket
            log.warning("update_bracket failed (%s); re-placing", e)
            self.x.cancel_all(pos.symbol)
            pos.stop = new_stop
            self._protect(pos)
        pos.stop = new_stop
        self.store.save_position(pos)
        self._audit(AuditEvent.ORDER_UPDATE, proposal_id=pos.proposal_id, stop_moved=[old, new_stop],
                    reason=why)
        return True

    def _fee_rate(self, maker: bool = False) -> float:
        c = self.cfg.costs.crypto
        return (c.maker_fee_pct if maker else c.taker_fee_pct) / 100 * (1 + c.gst_pct / 100)

    def _submit(self, o: ManagedOrder) -> ManagedOrder:
        """Place with idempotent retries. Never resends an order the exchange already has."""
        self._audit(AuditEvent.ORDER_SUBMITTED, proposal_id=o.proposal_id, client_id=o.client_id,
                    kind=o.kind.value, symbol=o.symbol, side=o.side, size=o.size,
                    order_type=o.order_type, limit_price=o.limit_price, reduce_only=o.reduce_only)
        for attempt in range(self.ec.order_retries + 1):
            o.attempts = attempt + 1
            try:
                o.apply(self.x.place_order(o))
                break
            except (ExchangeError, httpx.TransportError) as e:
                if isinstance(e, ExchangeError) and not e.retryable:
                    o.state, o.error = OrderState.REJECTED, str(e)
                    break
                log.warning("order %s attempt %d failed: %s; checking exchange", o.client_id,
                            attempt + 1, e)
                existing = self.x.get_order(o.client_id, o.symbol)
                if existing is not None:          # it DID reach the exchange: adopt, don't resend
                    o.apply(existing)
                    break
                self.sleep(min(2 ** attempt, 8))
        else:
            o.state, o.error = OrderState.REJECTED, "gave up after retries"
        self.store.save_order(o)
        ev = AuditEvent.ORDER_REJECTED if o.state is OrderState.REJECTED else AuditEvent.ORDER_UPDATE
        self._audit(ev, proposal_id=o.proposal_id, client_id=o.client_id, state=o.state.value,
                    filled=o.filled, avg_price=o.avg_price, error=o.error, exchange_id=o.exchange_id)
        return o

    # ---------- entry ----------

    def execute(self, p: Proposal, sig: Signal, live_price: float) -> str:
        spec = self.specs[p.symbol]
        if any(q.symbol == p.symbol for q in self.store.positions()):
            raise ExecutionError(f"already holding {p.symbol}")
        lev = self.x.set_leverage(p.symbol, p.order_leverage)
        if abs(lev - p.order_leverage) > 1e-9:
            raise ExecutionError(f"leverage is {lev}x, expected {p.order_leverage}x: not trading")

        buy = p.side == "buy"
        cap = self.ec.max_entry_slippage_pct / 100
        limit = round_to_tick(live_price * (1 + cap if buy else 1 - cap), spec.tick_size, up=buy)
        entry = self._submit(ManagedOrder(p.id, p.symbol, spec.product_id, OrderKind.ENTRY,
                                          p.side, p.contracts, "limit", limit))
        if entry.state is OrderState.REJECTED:
            raise ExecutionError(f"entry rejected: {entry.error}")
        if entry.filled == 0:
            raise ExecutionError(f"entry not filled: price moved beyond {limit} (IOC cap)")

        now = self.now()
        x = self.cfg.exits
        atr_pct = float((sig.features or {}).get("atr_pct") or 0) if sig is not None else 0.0
        pos = ManagedPosition(
            p.id, p.symbol, spec.product_id, p.side, entry.filled, spec.contract_value,
            entry.avg_price, p.stop, p.target, now.isoformat(),
            (now + timedelta(hours=p.max_hold_hours or 72)).isoformat(), p.strategy,
            p.risk_inr * entry.filled / max(p.contracts, 1), horizon=p.horizon,
            initial_stop=p.stop,
            trail_dist=x.trail_atr_mult * atr_pct * entry.avg_price if x.trailing_enabled else 0.0)
        self.store.save_position(pos)
        self._audit(AuditEvent.FILL, proposal_id=p.id, symbol=p.symbol, side=p.side,
                    contracts=entry.filled, requested=p.contracts, price=entry.avg_price)
        if entry.filled < p.contracts:
            self.notify(f"#{p.id} partial fill: {entry.filled}/{p.contracts} contracts")
        self._protect(pos)
        self.notify(f"#{p.id} FILLED {pos.contracts} {p.symbol} {'long' if buy else 'short'} "
                    f"@ {pos.entry_price:,.2f}; exchange stop {pos.stop:,.2f}, target {pos.target:,.2f}")
        return entry.client_id

    def _protect(self, pos: ManagedPosition) -> None:
        for attempt in range(self.ec.bracket_retries):
            try:
                self.x.place_bracket(pos.symbol, pos.side, pos.stop, self._exchange_target(pos))
                if self.x.has_bracket(pos.symbol):
                    pos.bracket_ok = True
                    self.store.save_position(pos)
                    self._audit(AuditEvent.ORDER_UPDATE, proposal_id=pos.proposal_id,
                                bracket="placed", stop=pos.stop, target=pos.target)
                    return
            except Exception as e:  # noqa: BLE001 - every failure path ends in a flat position
                log.error("bracket attempt %d for %s failed: %s", attempt + 1, pos.symbol, e)
            self.sleep(1 + attempt)
        msg = f"#{pos.proposal_id} could NOT place exchange stop for {pos.symbol}: closing now"
        self.notify("ALERT " + msg)
        self._audit(AuditEvent.ERROR, proposal_id=pos.proposal_id, error=msg)
        self.close(pos.proposal_id, "no exchange stop possible")
        raise ExecutionError(msg)

    # ---------- exits ----------

    def close(self, proposal_id: str, reason: str) -> bool:
        """Market reduce-only close of a bot position (time exit, manual, kill switch)."""
        pos = self.store.position(proposal_id)
        if not pos or pos.state is PositionState.CLOSED:
            return False
        pos.state = PositionState.CLOSING
        self.store.save_position(pos)
        self.x.cancel_all(pos.symbol)                      # remove the bracket first
        info = self.x.position(pos.symbol)
        if info.size == 0:
            self._finalize(pos, self.x.last_exit_price(pos.symbol) or self.x.last_price(pos.symbol),
                           "already flat on exchange")
            return True
        side = "sell" if info.size > 0 else "buy"
        o = self._submit(ManagedOrder(pos.proposal_id, pos.symbol, pos.product_id, OrderKind.EXIT,
                                      side, abs(info.size), "market", reduce_only=True))
        if o.state is not OrderState.FILLED:
            self.notify(f"ALERT close of {pos.symbol} not confirmed ({o.state.value}: {o.error})")
            self.risk_store.halt(HaltKind.RECONCILE, f"close of {pos.symbol} failed: {o.error}")
            return False
        self._finalize(pos, o.avg_price, reason)
        return True

    def flatten_all(self, reason: str) -> list[str]:
        out = []
        for pos in self.store.positions():
            ok = self.close(pos.proposal_id, reason)
            out.append(f"{pos.symbol}: {'closed' if ok else 'FAILED'}")
        for sym in self.specs:
            self.x.cancel_all(sym)
        return out

    def _finalize(self, pos: ManagedPosition, exit_px: float, reason: str) -> None:
        sgn = 1 if pos.side == "buy" else -1
        qty = pos.contracts * pos.contract_value
        maker = reason == "target"
        fees = qty * (pos.entry_price * self._fee_rate() + exit_px * self._fee_rate(maker))
        pos.pnl_usd = sgn * (exit_px - pos.entry_price) * qty - fees
        pos.exit_price, pos.exit_reason = exit_px, reason
        pos.closed_at, pos.state = self.now().isoformat(), PositionState.CLOSED
        self.store.save_position(pos)
        pnl_inr = pos.pnl_usd * self.cfg.crypto.usd_inr
        st = self.risk_store.state()
        self.risk_store.record_trade(pnl_inr, pos.symbol, pos.strategy,
                                     pnl_inr / pos.risk_inr if pos.risk_inr else None)
        self.risk_store.mark_equity(st.equity_inr + pnl_inr)
        self._audit(AuditEvent.POSITION_CLOSED, proposal_id=pos.proposal_id, symbol=pos.symbol,
                    side=pos.side, contracts=pos.contracts, entry=pos.entry_price, exit=exit_px,
                    reason=reason, pnl_usd=round(pos.pnl_usd, 4), pnl_inr=round(pnl_inr, 2),
                    fees_usd=round(fees, 4))
        self.notify(f"#{pos.proposal_id} CLOSED {pos.symbol} ({reason}) @ {exit_px:,.2f}: "
                    f"P&L ₹{pnl_inr:+,.0f}")

    # ---------- reconciliation ----------

    def reconcile(self) -> list[str]:
        """Bot's view vs the exchange's. Fixes what it safely can, halts + alerts otherwise."""
        events = []
        if hasattr(self.x, "poll"):
            self.x.poll()                                  # paper: fire triggered brackets
        managed = {p.symbol: p for p in self.store.positions()}
        for sym in self.specs:
            info = self.x.position(sym)
            pos = managed.get(sym)
            if pos is None:
                if info.size != 0:
                    msg = f"ORPHAN position on exchange: {sym} {info.size} contracts (not opened by bot)"
                    events.append(msg)
                    self.risk_store.halt(HaltKind.RECONCILE, msg)
                continue
            if info.size == 0:
                px = self.x.last_exit_price(sym) or self.x.last_price(sym)
                why = "target" if abs(px - pos.target) < abs(px - pos.stop) else "stop"
                self._finalize(pos, px, why)
                events.append(f"{sym}: closed on exchange ({why})")
            elif info.size != pos.signed:
                msg = f"{sym}: size mismatch bot {pos.signed} vs exchange {info.size}"
                events.append(msg)
                self.risk_store.halt(HaltKind.RECONCILE, msg)
                self._audit(AuditEvent.ERROR, proposal_id=pos.proposal_id, error=msg)
            elif not self.x.has_bracket(sym):
                events.append(f"{sym}: exchange stop missing; re-placing")
                self._audit(AuditEvent.ERROR, proposal_id=pos.proposal_id, error="bracket missing")
                try:
                    self._protect(pos)
                except ExecutionError as e:
                    events.append(str(e))
        for e in events:
            self.notify("RECONCILE " + e)
        return events


def deadline(pos: ManagedPosition) -> datetime:
    return datetime.fromisoformat(pos.exit_by)
