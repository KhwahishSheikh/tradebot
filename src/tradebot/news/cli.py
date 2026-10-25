"""`tradebot news ...`: update (fetch+classify), digest, score."""

from __future__ import annotations

import argparse

from tradebot.config import load_config
from tradebot.news import pipeline
from tradebot.news.score import NewsScorer
from tradebot.news.store import NewsStore
from tradebot.timeutil import now_utc


def cmd_update(args, cfg, secrets) -> int:
    store = NewsStore(cfg.news.db_path)
    print(pipeline.run(cfg, secrets, store).line())
    n, m = store.counts()
    print(f"store: {n} items, {m} labeled")
    store.close()
    return 0


def cmd_digest(args, cfg, secrets) -> int:
    extra = {}
    try:  # add regime + funding context if market data is present
        from tradebot.data.store import MarketStore
        from tradebot.strategies.engine import SignalEngine
        ms = MarketStore(cfg.data.db_path)
        eng = SignalEngine(cfg, ms)
        for sym in cfg.crypto.symbols:
            f = eng.features(sym, "1h")
            r = f.iloc[-1]
            extra[sym] = (f"price {r.close:,.1f}, regime {r.regime}, funding {r.funding:+.4f}%/8h")
        ms.close()
    except Exception as e:  # noqa: BLE001 - digest still useful without market context
        print(f"(no market context: {e})")
    store = NewsStore(cfg.news.db_path)
    print(pipeline.digest(cfg, store, args.hours, extra))
    store.close()
    return 0


def cmd_score(args, cfg, secrets) -> int:
    store = NewsStore(cfg.news.db_path)
    scorer = NewsScorer(store, cfg.news.lookback_hours)
    for sym in cfg.crypto.symbols:
        print(sym, scorer.context(sym, now_utc()).line())
    store.close()
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("news", help="news & sentiment")
    ns = p.add_subparsers(dest="news_cmd", required=True)
    ns.add_parser("update", help="fetch feeds + classify new items").set_defaults(news_fn=cmd_update)
    d = ns.add_parser("digest", help="watchlist digest text")
    d.add_argument("--hours", type=int, default=12)
    d.set_defaults(news_fn=cmd_digest)
    ns.add_parser("score", help="current news score per symbol").set_defaults(news_fn=cmd_score)
    p.set_defaults(fn=lambda args: args.news_fn(args, *load_config(args.config_dir, args.env_file)))
