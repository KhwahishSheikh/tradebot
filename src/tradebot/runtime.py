"""The bot process: one main thread owns all state; a Telegram thread only fetches updates.

Schedule (all times UTC unless noted):
  every 1s          handle Telegram updates (approve / reject / commands)
  every tick (60s)  reconcile with exchange, exits (max hold, trailing, questions), expire proposals
  bar close + 75s   refresh data -> health check -> strategies -> proposals (risk-checked)
  every 30 min      news update
  review time IST   position review + news digest
  summary time IST  daily summary
  every 15s         heartbeat file (watched by `tradebot watchdog` from outside)
Safe mode (no new entries, exits keep running) when data is stale or ticks keep failing; it
clears itself when healthy again.
"""

from __future__ import annotations

import json
import logging
import queue
import signal
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from tradebot.audit import AuditEvent, AuditLog
from tradebot.config import AppConfig, Secrets
from tradebot.core.models import Market
from tradebot.data.store import MarketStore
from tradebot.execution.exits import PositionManager
from tradebot.execution.factory import build_execution
from tradebot.monitoring.health import daily_summary, next_daily, write_heartbeat
from tradebot.paper.ledger import ForwardLedger
from tradebot.proposals.engine import ConsoleNotifier, ProposalEngine
from tradebot.proposals.model import render
from tradebot.proposals.store import ProposalStore
from tradebot.risk.engine import MarketContext
from tradebot.risk.state import HaltKind
from tradebot.timeutil import IST, now_utc
from tradebot.timing.clock import TradingClock

log = logging.getLogger("tradebot.runtime")


class Runtime:
    def __init__(self, cfg: AppConfig, secrets: Secrets, clock: Callable[[], datetime] = now_utc,
                 telegram_api=None):
        from tradebot.risk.cli import build_manager
        self.cfg, self.secrets, self.now = cfg, secrets, clock
        self.m = cfg.monitoring
        self.audit = AuditLog(cfg.logging.dir, cfg.mode)
        self.risk, self.delta = build_manager(cfg, secrets)
        self.started = clock()
        self.stop_flag = False
        self.updates: queue.Queue = queue.Queue()
        self.last_alert: dict[str, datetime] = {}
        self.tick_failures = 0
        self.state = self._load_state()

        token = secrets.telegram_bot_token.get_secret_value() if secrets.telegram_bot_token else None
        self.tg_api, self.poller, self.router = None, None, None
        if (token or telegram_api) and cfg.telegram.allowed_user_ids:
            from tradebot.monitoring.telegram import Poller, TelegramAPI, TelegramNotifier
            self.tg_api = telegram_api or TelegramAPI(token)
            self.notifier = TelegramNotifier(self.tg_api, cfg.telegram.allowed_user_ids)
            self.poller = Poller(self.tg_api, self.updates)
        else:
            self.notifier = ConsoleNotifier()
            log.warning("Telegram not configured (TELEGRAM_BOT_TOKEN + telegram.allowed_user_ids): "
                        "proposals go to the console; approve with `tradebot proposals approve ID`")

        self.execu = build_execution(cfg, secrets, self.risk.engine.specs, self.risk.store, self.audit,
                                     notify=self.notifier.message)
        self.risk.open_positions = self.execu.open_risk
        self.proposals = ProposalEngine(cfg, self.risk, ProposalStore(cfg.data.db_path.parent / "proposals.db"),
                                        lambda s: float(self.delta.last_price(s)), self.execu,
                                        self.audit, self.notifier, clock)
        self.ledger = ForwardLedger(cfg.paper.ledger_path)
        self.ledger.started()                                  # first run marks the paper start
        self.positions = PositionManager(
            cfg, self.execu, self.notifier.message, clock, features=self._features_1h,
            news=None, trading_clock=TradingClock(cfg),
            on_question=getattr(self.notifier, "question", None))
        if self.tg_api is not None:
            from tradebot.monitoring.bot import Router, Services
            self.router = Router(Services(cfg, secrets, self.proposals, self.positions, self.risk,
                                          self.audit, self.status_text, self.summary_text,
                                          self.digest_text, self.paper_text), self.tg_api,
                                 self.notifier, clock)

        now = clock()
        self.next = {"tick": now, "signals": self._next_bar_job(now), "news": now,
                     "review": next_daily(cfg.exits.review_time_ist, now),
                     "summary": next_daily(self.m.summary_time_ist, now), "heartbeat": now}

    # ---------- persistence of runtime state ----------

    def _load_state(self) -> dict:
        try:
            return json.loads(self.m.state_file.read_text())
        except (OSError, ValueError):
            return {"evaluated": {}}

    def _save_state(self) -> None:
        self.m.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.m.state_file.write_text(json.dumps(self.state, default=str))

    # ---------- helpers ----------

    def _next_bar_job(self, now: datetime) -> datetime:
        top = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return top + timedelta(seconds=self.m.signal_delay_seconds)

    def _features_1h(self, symbol: str):
        from tradebot.strategies.engine import SignalEngine
        ms = MarketStore(self.cfg.data.db_path)
        try:
            return SignalEngine(self.cfg, ms).features(symbol, "1h")
        finally:
            ms.close()

    def alert(self, key: str, text: str) -> None:
        last = self.last_alert.get(key)
        if last and self.now() - last < timedelta(minutes=self.m.alert_cooldown_min):
            return
        self.last_alert[key] = self.now()
        self.notifier.message("⚠️ " + text)

    def _job(self, name: str, fn) -> bool:
        try:
            fn()
            return True
        except Exception as e:
            log.exception("job %s failed", name)
            self.audit.record(AuditEvent.ERROR, where=f"runtime.{name}", error=repr(e))
            self.alert(name, f"{name} failed: {e}")
            return False

    def _safe_mode(self, unhealthy: bool, why: str) -> None:
        active = any(h.kind is HaltKind.SAFE_MODE for h in self.risk.store.active_halts())
        if unhealthy and not active:
            self.risk.store.halt(HaltKind.SAFE_MODE, why)
            self.alert("safe_mode", f"SAFE MODE ON: {why}. No new entries; stops/exits still active.")
        elif not unhealthy and active:
            self.risk.store.resume("runtime", {HaltKind.SAFE_MODE})
            self.notifier.message("✅ Safe mode cleared: healthy again.")

    # ---------- jobs ----------

    def tick(self) -> None:
        for p in self.proposals.expire_due():
            if hasattr(self.notifier, "finalize"):
                self.notifier.finalize(p.id, render(p, self.now()))
        ok = self._job("positions", self.positions.tick)
        self.tick_failures = 0 if ok else self.tick_failures + 1
        if self.tick_failures >= self.m.max_tick_failures:
            self._safe_mode(True, f"{self.tick_failures} consecutive reconcile/exit failures")

    def signals(self) -> None:
        from tradebot.data.ingest import Ingestor
        from tradebot.strategies.engine import SignalEngine
        from tradebot.timing.nse import NseCalendar
        cfg = self.cfg
        ms = MarketStore(cfg.data.db_path)
        try:
            ing = Ingestor(ms, NseCalendar.from_yaml(cfg.timing.nse.holidays_file))
            for sym in cfg.crypto.symbols:
                for s in (sym, f"FUNDING:{sym}", f"MARK:{sym}", f"OI:{sym}"):
                    ing.backfill(self.delta, s, "1h", 3, check_gaps=False)
                ing.backfill(self.delta, sym, "5m", 4, check_gaps=False)   # forward-test outcomes
            last = min(ms.last_ts(Market.CRYPTO_DELTA, s, "1h") for s in cfg.crypto.symbols)
            stale = (self.now() - last) > timedelta(hours=self.m.stale_data_hours)
            self._safe_mode(stale, f"market data stale (last bar {last:%H:%M} UTC)")
            if stale:
                return
            news = None
            if cfg.news.enabled and cfg.news.use_in_signals:
                from tradebot.news.score import NewsScorer
                from tradebot.news.store import NewsStore
                news = NewsScorer(NewsStore(cfg.news.db_path), cfg.news.lookback_hours)
            from tradebot.paper.evaluate import evaluate_outcomes
            evaluate_outcomes(cfg, self.ledger, ms, self.now())
            for ev in SignalEngine(cfg, ms, news).latest(cfg.paper.shadow_disabled_strategies):
                key = f"{ev.strategy}:{ev.symbol}"
                if self.state["evaluated"].get(key) == ev.ts.isoformat():
                    continue                                   # this bar already handled
                self.state["evaluated"][key] = ev.ts.isoformat()
                self.audit.record(AuditEvent.SIGNAL, strategy=ev.strategy, symbol=ev.symbol,
                                  side=ev.setup.side.value, passed=ev.passed, blocked_by=ev.blocked_by,
                                  confidence=ev.confidence, net_rr=round(ev.net_rr, 2), regime=ev.regime,
                                  shadow=not ev.enabled)
                if not ev.passed:
                    continue
                bar_close = ev.signal.created_at
                self.ledger.record(ev.signal, bar_close, ev.enabled)
                if not ev.enabled:
                    continue                                   # shadow: track only, never trade
                out = self.proposals.create(ev.signal, MarketContext(self.now(), None, bar_close))
                self.ledger.link(ev.signal.id, out.proposal.id if out.ok and out.proposal else None,
                                 out.proposal.status.value if out.ok and out.proposal else
                                 f"not proposed: {out.message}")
                if not out.ok:
                    log.info("signal %s not proposed: %s %s", key, out.message, out.reasons)
            self.ledger.sync_statuses(lambda pid: (g := self.proposals.store.get(pid)) and g[0].status.value)
            self._save_state()
        finally:
            ms.close()

    def news(self) -> None:
        from tradebot.news import pipeline
        from tradebot.news.store import NewsStore
        st = NewsStore(self.cfg.news.db_path)
        try:
            pipeline.run(self.cfg, self.secrets, st)
        finally:
            st.close()

    def digest_text(self) -> str:
        from tradebot.news import pipeline
        from tradebot.news.store import NewsStore
        st = NewsStore(self.cfg.news.db_path)
        try:
            return pipeline.digest(self.cfg, st)
        finally:
            st.close()

    def summary_text(self) -> str:
        return daily_summary(self.cfg, self.proposals.store, self.execu.store, self.risk.store)

    def paper_text(self) -> str:
        from tradebot.paper.report import build_report, short
        text, _ = build_report(self.cfg, self.ledger, self.proposals.store, self.execu.store,
                               self.risk.store)
        return short(text)

    def status_text(self) -> str:
        st = self.risk.store.state()
        up = self.now() - self.started
        poll = f", telegram poll {time.time() - self.poller.last_ok:.0f}s ago" if self.poller else ""
        return (f"🤖 {self.cfg.mode.value.upper()} | up {up.total_seconds() / 3600:.1f}h{poll}\n"
                f"next signals {self.next['signals'].astimezone(IST):%H:%M:%S} IST | "
                f"open positions {len(self.execu.store.positions())} | "
                f"pending proposals {len(self.proposals.store.pending())}\n"
                f"equity ₹{st.equity_inr:,.0f} | today {st.day_pnl_pct:+.2f}% | DD {st.drawdown_pct:.2f}%\n"
                + ("halts: " + ", ".join(f"{h.kind.value}" for h in st.halts) if st.halts else "no halts"))

    # ---------- main loop ----------

    def step(self) -> None:
        """One iteration (also what tests drive)."""
        while not self.updates.empty() and self.router:
            self._job("telegram", lambda: self.router.handle(self.updates.get_nowait()))
        now = self.now()
        if now >= self.next["tick"]:
            self.tick()
            self.next["tick"] = now + timedelta(seconds=self.m.tick_seconds)
        if now >= self.next["signals"]:
            self._job("signals", self.signals)
            self.next["signals"] = self._next_bar_job(now)
        if now >= self.next["news"] and self.cfg.news.enabled:
            self._job("news", self.news)
            self.next["news"] = now + timedelta(minutes=self.m.news_minutes)
        if now >= self.next["review"]:
            self._job("review", lambda: self.notifier.message(self.positions.daily_review()
                                                              + "\n\n" + self.digest_text()))
            self.next["review"] = next_daily(self.cfg.exits.review_time_ist, now)
        if now >= self.next["summary"]:
            self._job("summary", lambda: self.notifier.message(self.summary_text()))
            self.next["summary"] = next_daily(self.m.summary_time_ist, now)
        if now >= self.next["heartbeat"]:
            write_heartbeat(self.m.heartbeat_file, mode=self.cfg.mode.value,
                            next_signals=self.next["signals"], tick_failures=self.tick_failures)
            self.ledger.beat(now)
            self.next["heartbeat"] = now + timedelta(seconds=15)

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop_flag", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stop_flag", True))
        self.audit.record(AuditEvent.SYSTEM_START, mode=self.cfg.mode.value,
                          strategies=[e.name for e in self.cfg.strategies.crypto if e.enabled])
        if self.poller:
            self.poller.start()
        self.notifier.message(f"🤖 tradebot started ({self.cfg.mode.value}). /help for commands.")
        try:
            while not self.stop_flag:
                self.step()
                time.sleep(1)
        finally:
            if self.poller:
                self.poller.stop_event.set()
            self.audit.record(AuditEvent.SYSTEM_STOP, reason="signal")
            self.notifier.message("🤖 tradebot stopped. Exchange-side stops remain in place.")
