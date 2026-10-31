"""delta-news: recent crypto/gold headlines for your watchlist from public RSS feeds (no keys).

  python news.py                 # watchlist from rules.json, last 12 hours
  python news.py BTCUSD --hours 24

Flags HIGH-impact words (hack, ETF, SEC, Fed, CPI, liquidation...). Headlines only — read the
article before acting, and remember big news often moves price before the headline arrives.
"""
import argparse
import re
import xml.etree.ElementTree as ET
from datetime import timedelta
from email.utils import parsedate_to_datetime

import requests

from delta_common import UK, load_rules, now_utc, uk_utc

FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
}
WORDS = {
    "BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "ether", "eth"], "SOL": ["solana", "sol"],
    "XRP": ["xrp", "ripple"], "DOGE": ["dogecoin", "doge"], "XAUT": ["gold", "xaut", "tether gold"],
}
MACRO = ["fed", "fomc", "powell", "cpi", "inflation", "rate cut", "rate hike", "tariff", "jobs report"]
HIGH = ["hack", "exploit", "etf", "sec ", "lawsuit", "ban", "liquidat", "crash", "surge", "plunge",
        "fed", "fomc", "cpi", "bankrupt", "halt", "delist", "approval"]


def fetch(name, url):
    r = requests.get(url, timeout=10, headers={"User-Agent": "delta-assistant/1.0"})
    r.raise_for_status()
    out = []
    for item in ET.fromstring(r.content).iter("item"):
        title = (item.findtext("title") or "").strip()
        try:
            when = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        out.append({"src": name, "title": title, "link": (item.findtext("link") or "").strip(), "when": when})
    return out


def main():
    rules = load_rules()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="*", default=rules["watchlist"])
    ap.add_argument("--hours", type=float, default=12)
    a = ap.parse_args()
    bases = [s.upper().replace("USD", "") for s in a.symbols]
    since = now_utc() - timedelta(hours=a.hours)
    items = []
    for name, url in FEEDS.items():
        try:
            items += fetch(name, url)
        except (requests.RequestException, ET.ParseError) as e:
            print(f"({name} unavailable: {type(e).__name__})")
    seen, rows = set(), []
    for it in sorted(items, key=lambda x: x["when"], reverse=True):
        if it["when"] < since or it["title"].lower() in seen:
            continue
        seen.add(it["title"].lower())
        t = " " + it["title"].lower() + " "
        tags = [b for b in bases if any(re.search(rf"\b{re.escape(w)}\b", t) for w in WORDS.get(b, [b.lower()]))]
        if any(w in t for w in MACRO):
            tags.append("MACRO")
        if not tags:
            continue
        rows.append((it, tags, any(w in t for w in HIGH)))
    print(f"News {uk_utc()}  last {a.hours:g}h  for {', '.join(bases)}  ({len(rows)} headlines)")
    for it, tags, high in rows:
        print(f"  {it['when'].astimezone(UK):%a %H:%M} UK  {'HIGH ' if high else '     '}[{'/'.join(tags)}] "
              f"{it['title']}  — {it['src']}\n        {it['link']}")
    if not rows:
        print("  nothing matching in this window")


if __name__ == "__main__":
    main()
