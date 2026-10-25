"""News classification: Claude (structured JSON output) or a keyword fallback.

Each label: assets affected, sentiment (-1..1), impact (0..3), time horizon, category.
News is a FILTER / confidence modifier only. Nothing here can create a trade.
"""

from __future__ import annotations

import json
import logging
import re
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, ValidationError

from tradebot.news.feeds import NewsItem

log = logging.getLogger(__name__)

ASSETS = ["BTC", "ETH", "CRYPTO_MARKET"]
CATEGORIES = ["regulation", "hack_exploit", "macro", "etf_flows", "adoption", "exchange",
              "tech_upgrade", "market_commentary", "other"]
HORIZONS = ["hours", "days", "weeks"]


class Label(BaseModel):
    id: str
    assets: list[Literal["BTC", "ETH", "CRYPTO_MARKET"]]
    sentiment: float = Field(ge=-1, le=1)
    impact: int = Field(ge=0, le=3)
    horizon: Literal["hours", "days", "weeks"]
    category: str
    rationale: str = ""


SYSTEM_PROMPT = """You label crypto news headlines for a risk-averse BTC/ETH perpetual-futures \
trading system. The labels adjust the confidence of trade ideas; they never create trades on \
their own, so precision matters more than recall.

For each item return:
- id: copied exactly from the input.
- assets: which of BTC, ETH, CRYPTO_MARKET the item is likely to move. CRYPTO_MARKET means \
broad crypto-wide impact (regulation, macro, major exchange events). Use [] if none of these \
(for example news about a small altcoin, an NFT project, or an individual).
- sentiment: expected price direction for those assets over the horizon, from -1 (strongly \
bearish) to 1 (strongly bullish). 0 if neutral or unclear.
- impact: 0 = noise or opinion; 1 = minor; 2 = notable, could move price a few percent; \
3 = major (large hack of a top exchange, a ban or approval by a major regulator, an emergency \
Fed action).
- horizon: how long the effect is likely to matter: hours, days, or weeks.
- category: one of regulation, hack_exploit, macro, etf_flows, adoption, exchange, \
tech_upgrade, market_commentary, other.
- rationale: at most 15 words.

Price-prediction articles, technical-analysis opinion pieces and sponsored posts are \
market_commentary with impact 0 or 1. Do not infer facts beyond the headline and summary."""

SCHEMA = {
    "type": "object",
    "properties": {"labels": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "assets": {"type": "array", "items": {"type": "string", "enum": ASSETS}},
            "sentiment": {"type": "number"},
            "impact": {"type": "integer", "enum": [0, 1, 2, 3]},
            "horizon": {"type": "string", "enum": HORIZONS},
            "category": {"type": "string", "enum": CATEGORIES},
            "rationale": {"type": "string"},
        },
        "required": ["id", "assets", "sentiment", "impact", "horizon", "category", "rationale"],
        "additionalProperties": False}}},
    "required": ["labels"],
    "additionalProperties": False,
}


def _validate(raw: list[dict], ids: set[str]) -> list[dict]:
    out = []
    for r in raw:
        if r.get("id") not in ids:
            continue  # never accept a label for an item we didn't send
        r["sentiment"] = max(-1.0, min(1.0, float(r.get("sentiment", 0))))
        try:
            out.append(Label(**r).model_dump())
        except ValidationError as e:
            log.warning("dropping invalid label %s: %s", r.get("id"), e)
    return out


class ClaudeClassifier:
    name = "claude"

    def __init__(self, model: str, effort: str = "low", client=None):
        import anthropic
        self.client = client or anthropic.Anthropic()
        self.model, self.effort = model, effort
        self.usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    def classify(self, items: list[NewsItem]) -> list[dict]:
        payload = [{"id": i.id, "source": i.source, "published": i.published.isoformat(),
                    "title": i.title, "summary": i.summary} for i in items]
        kwargs = {}
        if self.model in {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}:
            kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        resp = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            cache_control={"type": "ephemeral"},
            output_config={"effort": self.effort,
                           "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content":
                       "Label each news item.\n\n" + json.dumps(payload, ensure_ascii=False)}],
            **kwargs)
        self.usage["calls"] += 1
        self.usage["input_tokens"] += resp.usage.input_tokens
        self.usage["output_tokens"] += resp.usage.output_tokens
        if resp.stop_reason == "refusal":
            log.warning("classifier refused batch (%s); falling back to rules",
                        getattr(resp.stop_details, "category", None))
            return RulesClassifier().classify(items)
        if resp.stop_reason == "max_tokens":
            log.warning("classifier hit max_tokens; batch too big, partial labels dropped")
            return []
        text = next(b.text for b in resp.content if b.type == "text")
        return _validate(json.loads(text)["labels"], {i.id for i in items})


class RulesClassifier:
    """Keyword fallback: transparent and free, much cruder than the LLM. Used when no API key."""

    name = "rules"
    ASSET_PATTERNS: ClassVar[dict[str, str]] = {"BTC": r"\b(bitcoin|btc)\b", "ETH": r"\b(ethereum|ether|eth)\b"}
    MARKET = r"\b(crypto market|sec|cftc|fed|fomc|cpi|inflation|rate cut|rate hike|stablecoin|binance|coinbase|regulat\w*|tariff)\b"
    BULL: ClassVar[dict[str, float]] = {r"\b(surge|soar|rall(y|ies)|record high|all-time high|ath|approv\w*|inflows?|adopt\w*|buys?|bullish|rate cut|breakout)\b": 0.5}
    BEAR: ClassVar[dict[str, float]] = {r"\b(hack\w*|exploit\w*|drain\w*|stolen|ban\w*|lawsuit|sues?|charged|crash\w*|plunge\w*|outflows?|liquidat\w*|bearish|delist\w*|insolven\w*|bankrupt\w*|rate hike|sell-?off)\b": -0.5}
    MAJOR = r"\b(hack\w*|exploit\w*|ban\w*|etf|fomc|emergency|bankrupt\w*|insolven\w*)\b"
    COMMENTARY = r"\b(price prediction|could|might|analyst says|here's why|what's next|\?)"

    def classify(self, items: list[NewsItem]) -> list[dict]:
        out = []
        for it in items:
            text = f"{it.title} {it.summary}".lower()
            assets = [a for a, p in self.ASSET_PATTERNS.items() if re.search(p, text)]
            if re.search(self.MARKET, text):
                assets.append("CRYPTO_MARKET")
            s = sum(w for p, w in self.BULL.items() if re.search(p, text)) + sum(
                w for p, w in self.BEAR.items() if re.search(p, text))
            commentary = bool(re.search(self.COMMENTARY, it.title.lower()))
            impact = 0 if not assets else 2 if re.search(self.MAJOR, text) else 1
            if commentary:
                impact = min(impact, 1)
            out.append(Label(
                id=it.id, assets=sorted(set(assets)), sentiment=max(-1, min(1, s)), impact=impact,
                horizon="days", category="market_commentary" if commentary else "other",
                rationale="keyword rules").model_dump())
        return out


def make_classifier(kind: str, model: str, effort: str, api_key: str | None):
    """Claude only when a key is configured (.env ANTHROPIC_API_KEY); otherwise keyword rules."""
    if kind == "claude" and api_key:
        import anthropic
        return ClaudeClassifier(model, effort, anthropic.Anthropic(api_key=api_key))
    if kind == "claude":
        log.warning("ANTHROPIC_API_KEY not set; using keyword rules classifier")
    return RulesClassifier()
