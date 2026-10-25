"""News score per asset at a decision time, and how it nudges a setup's confidence.

score     = sum(sentiment x w) / sum(w), w = impact x exp(-age / tau[horizon]) x asset_weight
intensity = sum(w)   (how much relevant news there is at all)
Direct-asset items weigh 1.0; CRYPTO_MARKET items 0.6.

Confidence modifier is deliberately ASYMMETRIC (capital protection first):
  news strongly against the trade   -0.15     mildly against   -0.05
  news supporting the trade         +0.05     (never more: news is not a signal)
  any impact-3 item against the trade in the last 24h -> warning + -0.20
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from tradebot.core.models import Side
from tradebot.news.store import NewsStore

TAU_HOURS = {"hours": 6, "days": 48, "weeks": 168}
SYMBOL_ASSET = {"BTCUSD": "BTC", "ETHUSD": "ETH"}


@dataclass
class NewsContext:
    asset: str
    at: datetime
    score: float = 0.0
    intensity: float = 0.0
    n_items: int = 0
    major_adverse: list[str] = field(default_factory=list)   # filled per side by modifier()
    top: list[tuple[float, str, str]] = field(default_factory=list)  # (signed weight, title, src)
    _frame: pd.DataFrame | None = None

    def line(self) -> str:
        mood = "bullish" if self.score > 0.15 else "bearish" if self.score < -0.15 else "neutral"
        return f"news {mood} ({self.score:+.2f}, intensity {self.intensity:.1f}, {self.n_items} items)"


def score_frame(df: pd.DataFrame, asset: str, at: datetime) -> NewsContext:
    ctx = NewsContext(asset, at)
    if df.empty:
        return ctx
    rel = df[df["assets"].map(lambda a: asset in a or "CRYPTO_MARKET" in a)].copy()
    if rel.empty:
        return ctx
    age_h = (pd.Timestamp(at) - rel["published"]).dt.total_seconds() / 3600
    tau = rel["horizon"].map(TAU_HOURS).fillna(48)
    aw = rel["assets"].map(lambda a: 1.0 if asset in a else 0.6)
    rel["w"] = rel["impact"] * aw * (-(age_h.clip(lower=0)) / tau).map(math.exp)
    rel = rel[rel["w"] > 0.01]
    if rel.empty:
        return ctx
    ctx.intensity = float(rel["w"].sum())
    ctx.score = float((rel["sentiment"] * rel["w"]).sum() / max(ctx.intensity, 1.0))
    ctx.n_items = len(rel)
    rel["signed"] = rel["sentiment"] * rel["w"]
    top = rel.reindex(rel["signed"].abs().sort_values(ascending=False).index).head(5)
    ctx.top = [(float(r.signed), r.title, r.source) for r in top.itertuples()]
    ctx._frame = rel
    return ctx


def modifier(ctx: NewsContext, side: Side) -> tuple[float, list[str]]:
    """(confidence delta, notes) for a setup on `side` given the news context."""
    if ctx.n_items == 0:
        return 0.0, ["no relevant news"]
    sgn = 1 if side is Side.BUY else -1
    aligned = sgn * ctx.score
    notes = [ctx.line()]
    delta = 0.0
    if aligned <= -0.3 and ctx.intensity >= 2:
        delta = -0.15
    elif aligned <= -0.15:
        delta = -0.05
    elif aligned >= 0.3 and ctx.intensity >= 2:
        delta = 0.05
    f = ctx._frame
    if f is not None:
        recent = f[(f["impact"] >= 3) & (sgn * f["sentiment"] < 0)
                   & (f["published"] >= pd.Timestamp(ctx.at) - pd.Timedelta(hours=24))]
        if not recent.empty:
            delta = min(delta, -0.20)
            ctx.major_adverse = list(recent["title"].head(3))
            notes.append("MAJOR adverse news: " + " | ".join(ctx.major_adverse))
    return delta, notes


class NewsScorer:
    def __init__(self, store: NewsStore, lookback_hours: int = 72):
        self.store, self.lookback = store, timedelta(hours=lookback_hours)

    def context(self, symbol: str, at: datetime) -> NewsContext:
        asset = SYMBOL_ASSET.get(symbol, symbol)
        df = self.store.labeled(at - self.lookback, at)   # nothing published after `at`
        return score_frame(df, asset, at)
