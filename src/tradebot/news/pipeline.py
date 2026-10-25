"""News pipeline: fetch -> store -> classify NEW items only -> score / digest."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from tradebot.config import AppConfig, Secrets
from tradebot.news.classify import RulesClassifier, make_classifier
from tradebot.news.feeds import DEFAULT_FEEDS, fetch_all
from tradebot.news.score import NewsScorer
from tradebot.news.store import NewsStore
from tradebot.timeutil import IST, now_utc

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    fetched: int = 0
    new: int = 0
    labeled: int = 0
    classifier: str = ""
    feed_errors: dict[str, str] = field(default_factory=dict)
    usage: dict = field(default_factory=dict)

    def line(self) -> str:
        s = (f"fetched {self.fetched}, new {self.new}, labeled {self.labeled} "
             f"via {self.classifier}")
        if self.usage.get("calls"):
            u = self.usage
            s += f"  [{u['calls']} API calls, {u['input_tokens']:,} in / {u['output_tokens']:,} out tokens]"
        if self.feed_errors:
            s += f"  feed errors: {self.feed_errors}"
        return s


def run(cfg: AppConfig, secrets: Secrets, store: NewsStore) -> RunReport:
    n = cfg.news
    rep = RunReport()
    items, rep.feed_errors = fetch_all(n.feeds or DEFAULT_FEEDS)
    rep.fetched, rep.new = len(items), store.add_items(items)

    key = secrets.anthropic_api_key.get_secret_value() if secrets.anthropic_api_key else None
    clf = make_classifier(n.classifier, n.model, n.effort, key)
    rep.classifier = clf.name
    todo = store.unlabeled(now_utc() - timedelta(hours=n.classify_window_hours), n.max_items_per_run)
    for i in range(0, len(todo), n.batch_size):
        batch = todo[i:i + n.batch_size]
        try:
            labels = clf.classify(batch)
            store.add_labels(labels, clf.name)
        except Exception as e:  # noqa: BLE001 - API down / auth: degrade to rules, never crash
            log.error("classifier %s failed (%s); labeling batch with rules", clf.name, e)
            labels = RulesClassifier().classify(batch)
            store.add_labels(labels, "rules")
        rep.labeled += len(labels)
    rep.usage = getattr(clf, "usage", {})
    log.info("news run: %s", rep.line())
    return rep


def digest(cfg: AppConfig, store: NewsStore, hours: int = 12, extra: dict[str, str] | None = None
           ) -> str:
    """Pre-session watchlist text (sent to Telegram in Ticket 11)."""
    now = now_utc()
    scorer = NewsScorer(store, cfg.news.lookback_hours)
    lines = [f"Crypto news digest: {now.astimezone(IST):%a %d %b %H:%M} IST (last {hours}h)"]
    for sym in cfg.crypto.symbols:
        ctx = scorer.context(sym, now)
        lines.append(f"\n{sym}: {ctx.line()}" + (f"  |  {extra[sym]}" if extra and sym in extra else ""))
        for w, title, src in ctx.top[:4]:
            arrow = "▲" if w > 0 else "▼" if w < 0 else "•"
            lines.append(f"  {arrow} {title}  ({src})")
    recent = store.labeled(now - timedelta(hours=hours), now)
    big = recent[recent["impact"] >= 2] if not recent.empty else recent
    if not big.empty:
        lines.append("\nHigh-impact items:")
        for r in big.head(6).itertuples():
            lines.append(f"  [{r.category}, impact {r.impact}, {r.sentiment:+.1f}] {r.title}")
    lines.append("\nNews only adjusts confidence of strategy signals; it never triggers trades.")
    return "\n".join(lines)
