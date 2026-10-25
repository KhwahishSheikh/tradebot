"""Hold / exit logic for open positions.

Automatic (never waits for you): exchange stop + target, trailing stop moves, max-hold exit,
intraday square-off (NSE), kill switch, and the safe default when a question times out.
Asks you (and defaults to CLOSE if you don't answer in exits.decision_timeout_min):
  - extend_hold:    max hold reached while >= extend_min_r in profit -> hold longer?
  - target_reached: only when approval.exit_on_target = "ask": stop is locked at +half the
                    target distance first, then you choose close or keep riding.
Daily review: alerts only, never closes on its own (regime flip, adverse news, costly funding).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

import pandas as pd

from tradebot.audit import AuditEvent
from tradebot.config import AppConfig
from tradebot.core.models import Side
from tradebot.execution.engine import ExecutionEngine
from tradebot.execution.orders import ExitRequest, ManagedPosition, PositionState
from tradebot.timeutil import IST, now_utc

log = logging.getLogger(__name__)


class PositionManager:
    def __init__(self, cfg: AppConfig, execu: ExecutionEngine, notify: Callable[[str], None] = print,
                 clock: Callable[[], datetime] = now_utc,
                 features: Callable[[str], pd.DataFrame] | None = None, news=None,
                 trading_clock=None, on_question: Callable[[str, str], None] | None = None):
        self.cfg, self.x, self.e = cfg, cfg.exits, execu
        self.notify, self.now = notify, clock
        self.features, self.news, self.tclock = features, news, trading_clock
        self.on_question = on_question          # (request_id, text) -> e.g. Telegram buttons

    # ---------- helpers ----------

    def _audit(self, ev: AuditEvent, **data) -> None:
        self.e._audit(ev, **data)

    def authorized(self, user: str) -> bool:
        return user == "cli" or (user.isdigit() and int(user) in self.cfg.telegram.allowed_user_ids)

    def _pending_for(self, pos: ManagedPosition, kind: str) -> ExitRequest | None:
        return next((r for r in self.e.store.requests() if r.proposal_id == pos.proposal_id
                     and r.kind == kind), None)

    def _ask(self, pos: ManagedPosition, kind: str, detail: str) -> ExitRequest:
        now = self.now()
        r = ExitRequest(pos.proposal_id, kind, now.isoformat(),
                        (now + timedelta(minutes=self.x.decision_timeout_min)).isoformat(), detail)
        self.e.store.save_request(r)
        self._audit(AuditEvent.EXIT_REQUEST, request=r.__dict__)
        text = (f"QUESTION [{r.id}] {pos.symbol} #{pos.proposal_id}: {detail}\n"
                f"  reply HOLD or CLOSE within {self.x.decision_timeout_min} min "
                f"(no answer = {r.default_action.upper()})")
        (self.on_question or (lambda _rid, t: self.notify(t)))(r.id, text)
        return r

    # ---------- the loop body (run every minute or so) ----------

    def tick(self) -> list[str]:
        events = self.e.reconcile()
        now = self.now()
        for pos in self.e.store.positions((PositionState.OPEN,)):
            last = self.e.x.last_price(pos.symbol)
            r = pos.r_multiple(last)
            sgn = 1 if pos.side == Side.BUY.value else -1

            trail = pos.trail_dist > 0 and r >= self.x.trail_activate_r
            if trail and self.e.move_stop(pos, last - sgn * pos.trail_dist, f"trailing at {r:+.2f}R"):
                events.append(f"{pos.symbol}: trailing stop -> {pos.stop:,.2f}")

            if (self.cfg.approval.exit_on_target == "ask" and not pos.target_prompted
                    and sgn * (last - pos.target) >= 0):
                lock = pos.entry_price + sgn * abs(pos.target - pos.entry_price) / 2
                self.e.move_stop(pos, lock, "target reached: lock half the move")
                pos.target_prompted = True
                self.e.store.save_position(pos)
                self._ask(pos, "target_reached", f"target {pos.target:,.2f} reached ({r:+.2f}R). "
                          f"Stop moved to {pos.stop:,.2f}. Close now or keep riding?")
                events.append(f"{pos.symbol}: target reached, asked")

            nse_intraday = (self.tclock is not None and pos.horizon == "intraday"
                            and pos.symbol not in self.cfg.crypto.symbols)
            if nse_intraday and self.tclock.square_off_due(now):
                self.e.close(pos.proposal_id, "intraday square-off")
                events.append(f"{pos.symbol}: intraday square-off")
                continue

            if now >= datetime.fromisoformat(pos.exit_by) and not self._pending_for(pos, "extend_hold"):
                if r >= self.x.extend_min_r and pos.extensions < self.x.max_extensions:
                    self._ask(pos, "extend_hold", f"max hold reached at {r:+.2f}R. Hold "
                              f"{self.x.extend_hours}h longer (stop stays at {pos.stop:,.2f})?")
                    events.append(f"{pos.symbol}: max hold, asked to extend")
                else:
                    self.e.close(pos.proposal_id, f"max hold time ({r:+.2f}R)")
                    events.append(f"{pos.symbol}: closed at max hold")

        for req in self.e.store.requests():
            if now >= datetime.fromisoformat(req.expires_at):
                self._apply(req, req.default_action, "timeout")
                events.append(f"request {req.id} timed out -> {req.default_action}")
        return events

    # ---------- your answers ----------

    def answer(self, rid: str, action: str, user: str) -> str:
        if not self.authorized(user):
            self._audit(AuditEvent.UNAUTHORIZED_SENDER, user=user, action=action, target=rid)
            return "not authorized"
        req = self.e.store.request(rid)
        if not req or req.status != "pending":
            return f"no pending request {rid}"
        if self.now() >= datetime.fromisoformat(req.expires_at):
            self._apply(req, req.default_action, "timeout")
            return f"request {rid} had expired: default {req.default_action} applied"
        if action not in ("hold", "close"):
            return "answer must be hold or close"
        return self._apply(req, action, user)

    def _apply(self, req: ExitRequest, action: str, by: str) -> str:
        req.status = "timed_out" if by == "timeout" else "answered"
        req.answer, req.by = action, by
        self.e.store.save_request(req)
        self._audit(AuditEvent.EXIT_DECISION, request_id=req.id, proposal_id=req.proposal_id,
                    kind=req.kind, action=action, by=by)
        pos = self.e.store.position(req.proposal_id)
        if not pos or pos.state is not PositionState.OPEN:
            return "position already closed"
        if action == "close":
            self.e.close(pos.proposal_id, f"{req.kind}: close ({by})")
            return f"{pos.symbol} closed"
        if req.kind == "extend_hold":
            new = datetime.fromisoformat(pos.exit_by) + timedelta(hours=self.x.extend_hours)
            pos.exit_by, pos.extensions = new.isoformat(), pos.extensions + 1
            self.e.store.save_position(pos)
            return f"{pos.symbol} held until {new.astimezone(IST):%d %b %H:%M} IST"
        return f"{pos.symbol} kept open; stop {pos.stop:,.2f}"

    # ---------- daily review (alerts only) ----------

    def daily_review(self) -> str:
        now = self.now()
        positions = self.e.store.positions((PositionState.OPEN,))
        if not positions:
            return "Daily review: no open positions."
        lines = [f"Daily position review: {now.astimezone(IST):%a %d %b %H:%M} IST"]
        for pos in positions:
            last = self.e.x.last_price(pos.symbol)
            r = pos.r_multiple(last)
            sgn = 1 if pos.side == Side.BUY.value else -1
            held = now - datetime.fromisoformat(pos.opened_at)
            left = datetime.fromisoformat(pos.exit_by) - now
            flags = []
            if self.features is not None:
                f = self.features(pos.symbol)
                row = f.iloc[-1]
                atr = float(row["atr"])
                flags_regime = str(row["regime"])
                if (sgn > 0 and flags_regime == "trend_down") or (sgn < 0 and flags_regime == "trend_up"):
                    flags.append(f"regime flipped to {flags_regime} (thesis weakened)")
                dist = abs(last - pos.stop) / atr if atr else 0
                if dist < 0.5:
                    flags.append(f"price only {dist:.1f} ATR from stop")
                fr = float(row.get("funding", 0) or 0)
                paid_day = sgn * fr * 3
                if paid_day > self.x.adverse_funding_pct_day:
                    flags.append(f"funding costs {paid_day:.3f}%/day")
            if self.news is not None:
                from tradebot.news.score import modifier
                ctx = self.news.context(pos.symbol, now)
                delta, notes = modifier(ctx, Side(pos.side))
                if delta <= -0.15:
                    flags.append("news against position: " + "; ".join(notes))
            verdict = "REVIEW: consider closing" if flags else "HOLD (thesis intact)"
            lines.append(f"\n#{pos.proposal_id} {pos.symbol} {'LONG' if sgn > 0 else 'SHORT'} "
                         f"{pos.contracts}c @ {pos.entry_price:,.2f} | last {last:,.2f} | {r:+.2f}R | "
                         f"held {held.total_seconds() / 3600:.0f}h, {max(left.total_seconds(), 0) / 3600:.0f}h left")
            lines.append(f"  stop {pos.stop:,.2f}  target {pos.target:,.2f}  -> {verdict}")
            lines += [f"  ! {fl}" for fl in flags]
            self._audit(AuditEvent.POSITION_REVIEW, proposal_id=pos.proposal_id, r=round(r, 3),
                        flags=flags, verdict=verdict)
        lines.append("\nReview never closes positions by itself: use CLOSE if you agree.")
        return "\n".join(lines)
