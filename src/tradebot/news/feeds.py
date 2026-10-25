"""RSS ingestion. Only headline + publisher summary are stored (no full-article scraping):
that's what RSS is published for. Polite: one request per feed per run, identifying UA."""

from __future__ import annotations

import calendar
import hashlib
import html
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import feedparser
import httpx

log = logging.getLogger(__name__)

DEFAULT_FEEDS = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
    "decrypt": "https://decrypt.co/feed",
    "theblock": "https://www.theblock.co/rss.xml",
    "bitcoinmagazine": "https://bitcoinmagazine.com/.rss/full/",
    "cryptoslate": "https://cryptoslate.com/feed/",
}
_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class NewsItem:
    id: str
    source: str
    title: str
    url: str
    summary: str
    published: datetime   # UTC; fetch time if the feed gives none

    @staticmethod
    def make_id(url: str, title: str) -> str:
        return hashlib.sha1((url or title).strip().lower().encode()).hexdigest()[:16]


def _clean(text: str, limit: int = 400) -> str:
    text = re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", text or ""))).strip()
    return text[:limit]


def parse_feed(source: str, content: bytes, fetched_at: datetime) -> list[NewsItem]:
    parsed = feedparser.parse(content)
    items = []
    for e in parsed.entries:
        title = _clean(e.get("title", ""), 300)
        if not title:
            continue
        ts = e.get("published_parsed") or e.get("updated_parsed")
        published = datetime.fromtimestamp(calendar.timegm(ts), UTC) if ts else fetched_at
        url = e.get("link", "")
        items.append(NewsItem(NewsItem.make_id(url, title), source, title, url,
                              _clean(e.get("summary", "")), min(published, fetched_at)))
    return items


def fetch_all(feeds: dict[str, str], transport: httpx.BaseTransport | None = None
              ) -> tuple[list[NewsItem], dict[str, str]]:
    """Returns (items, errors_by_source). One bad feed never stops the others."""
    now = datetime.now(UTC)
    items, errors = [], {}
    with httpx.Client(timeout=20, follow_redirects=True, transport=transport,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; tradebot-rss/0.1)"}) as c:
        for source, url in feeds.items():
            try:
                r = c.get(url)
                r.raise_for_status()
                items += parse_feed(source, r.content, now)
            except Exception as e:  # noqa: BLE001 - log and continue with other feeds
                errors[source] = f"{type(e).__name__}: {e}"
                log.warning("feed %s failed: %s", source, e)
    return items, errors
