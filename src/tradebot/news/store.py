"""News store (separate DuckDB file so news polling never contends with market-data writes)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

from tradebot.news.feeds import NewsItem
from tradebot.timeutil import now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_items (
    id VARCHAR PRIMARY KEY, source VARCHAR, title VARCHAR, url VARCHAR, summary VARCHAR,
    published TIMESTAMPTZ, fetched TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS news_labels (
    id VARCHAR PRIMARY KEY, classifier VARCHAR, assets VARCHAR, sentiment DOUBLE,
    impact INTEGER, horizon VARCHAR, category VARCHAR, rationale VARCHAR, labeled TIMESTAMPTZ);
"""


class NewsStore:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect(str(path))
        self.db.execute("SET TimeZone = 'UTC'")
        self.db.execute(SCHEMA)

    def add_items(self, items: list[NewsItem]) -> int:
        """Insert new items; returns how many were new (same story from a re-poll is ignored)."""
        before = self.db.execute("SELECT count(*) FROM news_items").fetchone()[0]
        now = now_utc()
        for it in items:
            self.db.execute("INSERT OR IGNORE INTO news_items VALUES (?, ?, ?, ?, ?, ?, ?)",
                            [it.id, it.source, it.title, it.url, it.summary, it.published, now])
        return self.db.execute("SELECT count(*) FROM news_items").fetchone()[0] - before

    def unlabeled(self, since: datetime, limit: int) -> list[NewsItem]:
        rows = self.db.execute(
            "SELECT i.id, source, title, url, summary, epoch_ms(published) FROM news_items i "
            "LEFT JOIN news_labels l USING (id) WHERE l.id IS NULL AND published >= ? "
            "ORDER BY published DESC LIMIT ?", [since, limit]).fetchall()
        from datetime import UTC
        return [NewsItem(r[0], r[1], r[2], r[3], r[4], datetime.fromtimestamp(r[5] / 1000, UTC))
                for r in rows]

    def add_labels(self, labels: list[dict], classifier: str) -> None:
        now = now_utc()
        for lb in labels:
            self.db.execute(
                "INSERT OR REPLACE INTO news_labels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [lb["id"], classifier, json.dumps(sorted(lb["assets"])), lb["sentiment"],
                 lb["impact"], lb["horizon"], lb["category"], lb.get("rationale", ""), now])

    def labeled(self, since: datetime, until: datetime) -> pd.DataFrame:
        """Labeled items PUBLISHED in [since, until]: callers pass `until` = decision time,
        so a backtest can never see news from after the bar it is deciding on."""
        df = self.db.execute(
            "SELECT i.id, source, title, url, published, l.* EXCLUDE (id) FROM news_items i "
            "JOIN news_labels l USING (id) WHERE published >= ? AND published <= ? "
            "ORDER BY published DESC", [since, until]).df()
        if not df.empty:
            df["published"] = pd.to_datetime(df["published"], utc=True)
            df["assets"] = df["assets"].map(json.loads)
        return df

    def counts(self) -> tuple[int, int]:
        n = self.db.execute("SELECT count(*) FROM news_items").fetchone()[0]
        m = self.db.execute("SELECT count(*) FROM news_labels").fetchone()[0]
        return n, m

    def close(self) -> None:
        self.db.close()
