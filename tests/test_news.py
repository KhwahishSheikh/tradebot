import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pandas as pd
import pytest

from tradebot.core.models import Side
from tradebot.news.classify import ClaudeClassifier, RulesClassifier, make_classifier
from tradebot.news.feeds import NewsItem, fetch_all, parse_feed
from tradebot.news.score import NewsScorer, modifier, score_frame
from tradebot.news.store import NewsStore

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Bitcoin ETF sees record inflows</title><link>https://x/1</link>
<description>&lt;p&gt;Big &amp;amp; bold&lt;/p&gt;</description><pubDate>Fri, 16 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Ethereum exchange hacked, $200M drained</title><link>https://x/2</link>
<pubDate>Fri, 16 Oct 2026 11:00:00 GMT</pubDate></item>
<item><title></title><link>https://x/3</link></item>
</channel></rss>"""


def item(i, title, hours_ago=1, source="s"):
    return NewsItem(f"id{i}", source, title, f"https://x/{i}", "", NOW - timedelta(hours=hours_ago))


# ---------- feeds + store ----------

def test_parse_feed_cleans_html_and_skips_empty():
    items = parse_feed("src", RSS, NOW)
    assert [i.title for i in items] == ["Bitcoin ETF sees record inflows",
                                        "Ethereum exchange hacked, $200M drained"]
    assert items[0].summary == "Big & bold" and items[0].published == NOW - timedelta(hours=2)


def test_one_bad_feed_does_not_stop_others():
    def handler(req):
        return httpx.Response(500) if "bad" in str(req.url) else httpx.Response(200, content=RSS)
    items, errors = fetch_all({"good": "https://good/rss", "bad": "https://bad/rss"},
                              transport=httpx.MockTransport(handler))
    assert len(items) == 2 and "bad" in errors


def test_store_dedupes_and_never_returns_future_news(tmp_path):
    st = NewsStore(tmp_path / "n.duckdb")
    a, b = item(1, "old", 5), item(2, "future", -3)        # b published 3h AFTER NOW
    assert st.add_items([a, b]) == 2 and st.add_items([a]) == 0
    st.add_labels(RulesClassifier().classify([a, b]), "rules")
    df = st.labeled(NOW - timedelta(days=1), NOW)
    assert list(df["title"]) == ["old"]                    # no lookahead


# ---------- classifiers ----------

def test_rules_classifier():
    labels = {lb["id"]: lb for lb in RulesClassifier().classify([
        item(1, "Bitcoin ETF sees record inflows"),
        item(2, "Ethereum exchange hacked, $200M drained"),
        item(3, "Dogecoin price prediction: could it hit $1?"),
        item(4, "NFT artist launches collection")])}
    assert labels["id1"]["assets"] == ["BTC"] and labels["id1"]["sentiment"] > 0
    assert "ETH" in labels["id2"]["assets"] and labels["id2"]["sentiment"] < 0
    assert labels["id2"]["impact"] == 2
    assert labels["id3"]["impact"] <= 1 and labels["id4"]["impact"] == 0


class FakeClient:
    def __init__(self, response):
        self.calls = []
        outer = self

        class _M:
            def create(self, **kw):
                outer.calls.append(kw)
                return response
        self.beta = SimpleNamespace(messages=_M())


def fake_response(labels, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, stop_details=None,
                           content=[SimpleNamespace(type="text", text=json.dumps({"labels": labels}))],
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=300))


def test_claude_classifier_request_shape_and_validation():
    good = {"id": "id1", "assets": ["BTC"], "sentiment": 1.7, "impact": 2, "horizon": "days",
            "category": "etf_flows", "rationale": "inflows"}
    rogue = good | {"id": "not-sent"}
    client = FakeClient(fake_response([good, rogue]))
    clf = ClaudeClassifier("claude-opus-5-5", "low", client)
    out = clf.classify([item(1, "Bitcoin ETF sees record inflows")])
    assert [o["id"] for o in out] == ["id1"] and out[0]["sentiment"] == 1.0   # clipped, rogue dropped
    kw = client.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["output_config"]["effort"] == "low"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert clf.usage == {"input_tokens": 1000, "output_tokens": 300, "calls": 1}


def test_claude_refusal_falls_back_to_rules():
    clf = ClaudeClassifier("claude-opus-5-5", "low", FakeClient(fake_response([], "refusal")))
    out = clf.classify([item(1, "Bitcoin ETF sees record inflows")])
    assert out[0]["rationale"] == "keyword rules"


def test_no_key_means_rules():
    assert make_classifier("claude", "claude-opus-5-5", "low", None).name == "rules"


# ---------- score + modifier ----------

def frame(rows):
    df = pd.DataFrame(rows)
    df["published"] = pd.to_datetime(df["published"], utc=True)
    return df


def row(title, assets, s, impact, hours_ago, horizon="days"):
    return {"title": title, "source": "s", "assets": assets, "sentiment": s, "impact": impact,
            "horizon": horizon, "published": NOW - timedelta(hours=hours_ago)}


def test_score_decays_and_weights_assets():
    fresh = score_frame(frame([row("a", ["BTC"], -1, 2, 1)]), "BTC", NOW)
    stale = score_frame(frame([row("a", ["BTC"], -1, 2, 200)]), "BTC", NOW)
    assert fresh.intensity > stale.intensity and fresh.score < 0
    broad = score_frame(frame([row("m", ["CRYPTO_MARKET"], -1, 2, 1)]), "BTC", NOW)
    assert broad.intensity == pytest.approx(fresh.intensity * 0.6)
    other = score_frame(frame([row("e", ["ETH"], -1, 3, 1)]), "BTC", NOW)
    assert other.n_items == 0


def test_modifier_is_asymmetric_and_flags_major_news():
    bearish = score_frame(frame([row(f"b{i}", ["BTC"], -0.8, 2, 1) for i in range(3)]), "BTC", NOW)
    d_long, _ = modifier(bearish, Side.BUY)
    d_short, _ = modifier(bearish, Side.SELL)
    assert d_long == -0.15 and d_short == 0.05             # support never adds more than +0.05
    hack = score_frame(frame([row("Exchange hacked", ["CRYPTO_MARKET"], -0.9, 3, 2)]), "BTC", NOW)
    d, notes = modifier(hack, Side.BUY)
    assert d == -0.20 and any("MAJOR adverse" in n for n in notes)
    assert modifier(score_frame(frame([row("x", ["BTC"], 0, 1, 1)]).iloc[:0], "BTC", NOW), Side.BUY)[0] == 0


def test_scorer_uses_store_until_decision_time(tmp_path):
    st = NewsStore(tmp_path / "n.duckdb")
    items = [item(1, "Bitcoin exchange hacked", 2), item(2, "Bitcoin ETF approved", -1)]
    st.add_items(items)
    st.add_labels(RulesClassifier().classify(items), "rules")
    ctx = NewsScorer(st).context("BTCUSD", NOW)
    assert ctx.n_items == 1 and ctx.score < 0                # the future "approved" item is invisible
