"""Telegram command + button router. Runs on the runtime's main thread.

Security:
  - only telegram.allowed_user_ids get ANY response; others are audited and ignored
  - live mode + require_pin_live: APPROVE, /resume all and kill-switch confirm need the PIN
    (3 wrong PINs -> 15 min lockout); the PIN message is deleted from the chat after reading
  - /killswitch always asks for a confirm tap (fat-finger protection)
"""

from __future__ import annotations

import hmac
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from tradebot.audit import AuditEvent, AuditLog
from tradebot.config import AppConfig, Secrets
from tradebot.monitoring.telegram import TelegramAPI, TelegramNotifier
from tradebot.proposals.model import render
from tradebot.risk.state import HaltKind
from tradebot.timeutil import now_utc

log = logging.getLogger(__name__)

HELP = """Commands
/pending: open proposals + questions
/positions: open positions
/pnl: equity, today, drawdown
/review: position review now
/digest: news digest
/summary: today's summary
/paper: paper-phase progress + go-live checklist
/status: bot health + halts
/pause: stop new proposals (exits keep working)
/resume: clear pause/cooldown   (/resume all: clear every halt)
/killswitch: block entries + close all bot positions
/modify ID size=N sl=PRICE tp=PRICE"""


@dataclass
class Services:
    cfg: AppConfig
    secrets: Secrets
    proposals: object          # ProposalEngine
    positions: object          # PositionManager
    risk: object               # RiskManager
    audit: AuditLog | None
    status: Callable[[], str]
    summary: Callable[[], str]
    digest: Callable[[], str]
    paper: Callable[[], str] = lambda: "paper report unavailable"


class Router:
    def __init__(self, s: Services, api: TelegramAPI, notifier: TelegramNotifier,
                 clock: Callable[[], datetime] = now_utc):
        self.s, self.api, self.n, self.now = s, api, notifier, clock
        self.awaiting_pin: dict[int, tuple[str, str, datetime]] = {}   # user -> (action, arg, expiry)
        self.bad_pins: dict[int, list[datetime]] = {}

    # ---------- plumbing ----------

    def _audit(self, ev: AuditEvent, **d) -> None:
        if self.s.audit:
            self.s.audit.record(ev, **d)

    def _allowed(self, uid: int) -> bool:
        return uid in self.s.cfg.telegram.allowed_user_ids

    def _pin_required(self) -> bool:
        return self.s.cfg.is_live and self.s.cfg.telegram.require_pin_live

    def _reply(self, chat: int, text: str, buttons=None) -> None:
        self.api.send(chat, text, buttons)

    def handle(self, u: dict) -> None:
        if "callback_query" in u:
            cq = u["callback_query"]
            uid, chat, data = cq["from"]["id"], cq["message"]["chat"]["id"], cq.get("data", "")
            if not self._allowed(uid):
                return self._intruder(uid, f"button {data}")
            self.api.answer_callback(cq["id"])
            return self._button(uid, chat, data)
        msg = u.get("message") or {}
        if "from" not in msg:
            return None
        uid, chat, text = msg["from"]["id"], msg["chat"]["id"], (msg.get("text") or "").strip()
        if not self._allowed(uid):
            return self._intruder(uid, text[:40])
        if uid in self.awaiting_pin and not text.startswith("/"):
            return self._check_pin(uid, chat, msg.get("message_id"), text)
        return self._command(uid, chat, text)

    def _intruder(self, uid: int, what: str) -> None:
        self._audit(AuditEvent.UNAUTHORIZED_SENDER, telegram_user=uid, what=what)
        log.warning("ignored Telegram user %s (%s). If this is you, add %s to "
                    "telegram.allowed_user_ids in config/local.yaml", uid, what, uid)

    # ---------- PIN ----------

    def _need_pin(self, uid: int, chat: int, action: str, arg: str) -> None:
        self.awaiting_pin[uid] = (action, arg, self.now() + timedelta(minutes=2))
        self._reply(chat, "🔒 Live mode: send your PIN within 2 minutes to confirm.")

    def _check_pin(self, uid: int, chat: int, mid: int | None, text: str) -> None:
        action, arg, exp = self.awaiting_pin.pop(uid)
        if mid:
            try:
                self.api._call("deleteMessage", chat_id=chat, message_id=mid)
            except Exception:  # noqa: BLE001, S110 - best effort
                pass
        recent = [t for t in self.bad_pins.get(uid, []) if self.now() - t < timedelta(minutes=15)]
        if len(recent) >= 3:
            return self._reply(chat, "Too many wrong PINs: locked for 15 minutes.")
        if self.now() > exp:
            return self._reply(chat, "PIN window expired; tap the button again.")
        pin = self.s.secrets.telegram_approval_pin
        if not pin or not hmac.compare_digest(text, pin.get_secret_value()):
            self.bad_pins[uid] = [*recent, self.now()]
            self._audit(AuditEvent.UNAUTHORIZED_SENDER, telegram_user=uid, what="wrong PIN", action=action)
            return self._reply(chat, f"Wrong PIN ({len(recent) + 1}/3).")
        return self._do(uid, chat, action, arg)

    # ---------- buttons ----------

    def _button(self, uid: int, chat: int, data: str) -> None:
        kind, _, arg = data.partition(":")
        if kind == "ap" and self._pin_required():
            return self._need_pin(uid, chat, "approve", arg)
        if kind == "kc" and self._pin_required():
            return self._need_pin(uid, chat, "kill", arg)
        action = {"ap": "approve", "rj": "reject", "md": "modify_menu", "mh": "half", "mq": "quarter",
                  "xh": "hold", "xc": "close", "kc": "kill", "kx": "kill_cancel"}.get(kind)
        if action:
            self._do(uid, chat, action, arg)

    def _do(self, uid: int, chat: int, action: str, arg: str) -> None:
        user, P = str(uid), self.s.proposals
        if action in ("approve", "reject"):
            out = P.approve(arg, user) if action == "approve" else P.reject(arg, user, "via Telegram")
            text = ("✅ " if out.ok else "⛔ ") + out.message
            if out.reasons:
                text += "\n" + "\n".join(f"• {r}" for r in out.reasons)
            self._reply(chat, text)
            if out.proposal:
                self.n.finalize(arg, render(out.proposal, self.now()))
        elif action == "modify_menu":
            self._reply(chat, f"Modify #{arg}: pick a size, or send\n/modify {arg} size=N sl=PRICE tp=PRICE",
                        [[("½ size", f"mh:{arg}"), ("¼ size", f"mq:{arg}")]])
        elif action in ("half", "quarter"):
            got = P.store.get(arg)
            if not got:
                return self._reply(chat, "unknown proposal")
            n = max(1, got[0].contracts // (2 if action == "half" else 4))
            out = P.modify(arg, user, contracts=n)
            self._reply(chat, ("✏️ " if out.ok else "⛔ ") + out.message)
            if out.ok:
                self.n.finalize(arg, f"(superseded by v{out.proposal.version} below)")
                self.n.proposal(out.proposal, render(out.proposal, self.now()))
        elif action in ("hold", "close"):
            self._reply(chat, self.s.positions.answer(arg, action, user))
            self.n.finalize(arg, f"Answered: {action.upper()}")
        elif action == "kill":
            self.s.risk.kill_switch("Telegram /killswitch", by=user)
            lines = self.s.positions.e.flatten_all("kill switch (Telegram)")
            self._reply(chat, "🛑 KILL SWITCH ON. New entries blocked.\n" + ("\n".join(lines) or "no open positions"))
        elif action == "kill_cancel":
            self._reply(chat, "Kill switch cancelled.")
        elif action == "resume_all":
            n = self.s.risk.resume(user)
            self._reply(chat, f"Cleared {n} halt(s).")

    # ---------- commands ----------

    def _command(self, uid: int, chat: int, text: str) -> None:
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        user = str(uid)
        if cmd in ("/start", "/help"):
            return self._reply(chat, HELP)
        if cmd == "/pending":
            P = self.s.proposals
            P.expire_due()
            rows = [render(p, self.now()) for p in P.store.pending()]
            qs = [f"[{r.id}] {r.kind}: {r.detail}" for r in self.s.positions.e.store.requests()]
            return self._reply(chat, "\n\n".join(rows + qs) or "Nothing pending.")
        if cmd == "/positions":
            from tradebot.execution.cli import format_positions
            return self._reply(chat, format_positions(self.s.positions.e, self.s.cfg))
        if cmd == "/pnl":
            st = self.s.risk.store.state()
            return self._reply(chat, f"Equity ₹{st.equity_inr:,.0f} | today {st.day_pnl_pct:+.2f}% | "
                                     f"drawdown {st.drawdown_pct:.2f}% (peak ₹{st.peak_equity_inr:,.0f})")
        if cmd == "/review":
            return self._reply(chat, self.s.positions.daily_review())
        if cmd == "/digest":
            return self._reply(chat, self.s.digest())
        if cmd == "/paper":
            return self._reply(chat, self.s.paper())
        if cmd == "/summary":
            return self._reply(chat, self.s.summary())
        if cmd == "/status":
            return self._reply(chat, self.s.status())
        if cmd == "/pause":
            self.s.risk.store.halt(HaltKind.PAUSE, f"paused via Telegram by {user}")
            self._audit(AuditEvent.MODE_CHANGE, action="pause", by=user)
            return self._reply(chat, "⏸ Paused: no new proposals. Exits and stops keep working. /resume to continue.")
        if cmd == "/resume":
            if rest.strip() == "all":
                if self._pin_required():
                    return self._need_pin(uid, chat, "resume_all", "")
                return self._do(uid, chat, "resume_all", "")
            n = self.s.risk.resume(user, {HaltKind.PAUSE, HaltKind.COOLDOWN})
            return self._reply(chat, f"▶️ Resumed ({n} halt(s) cleared). Kill switch / drawdown / "
                                     "reconcile halts need /resume all.")
        if cmd == "/killswitch":
            return self._reply(chat, "Confirm KILL SWITCH? Blocks entries and closes ALL bot positions at market.",
                               [[("🛑 CONFIRM KILL", "kc:"), ("Cancel", "kx:")]])
        if cmd == "/modify":
            return self._modify_cmd(uid, chat, rest)
        return self._reply(chat, "Unknown command. /help")

    def _modify_cmd(self, uid: int, chat: int, rest: str) -> None:
        parts = rest.split()
        if not parts:
            return self._reply(chat, "usage: /modify ID size=N sl=PRICE tp=PRICE")
        pid, kv = parts[0], dict(re.findall(r"(\w+)=([\d.]+)", " ".join(parts[1:])))
        out = self.s.proposals.modify(pid, str(uid), contracts=int(kv["size"]) if "size" in kv else None,
                                      stop=float(kv["sl"]) if "sl" in kv else None,
                                      target=float(kv["tp"]) if "tp" in kv else None)
        text = ("✏️ " if out.ok else "⛔ ") + out.message
        if out.reasons:
            text += "\n" + "\n".join(f"• {r}" for r in out.reasons)
        self._reply(chat, text)
        if out.ok:
            self.n.finalize(pid, f"(superseded by v{out.proposal.version} below)")
            self.n.proposal(out.proposal, render(out.proposal, self.now()))
