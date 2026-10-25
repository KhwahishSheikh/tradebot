"""`tradebot data ...` commands: backfill, status, verify, snapshot."""

from __future__ import annotations

import argparse
from datetime import timedelta

from tradebot.config import AppConfig, Secrets, load_config
from tradebot.core.models import Market
from tradebot.data.ingest import Ingestor
from tradebot.data.quality import RESOLUTION_SECONDS, expected_grid, find_gaps
from tradebot.data.store import MarketStore
from tradebot.data.verify import check_expiries, check_funding_schedule
from tradebot.markets.angel.instruments import AngelInstruments
from tradebot.markets.delta.client import DeltaClient
from tradebot.timeutil import IST, now_utc
from tradebot.timing.crypto import CryptoClock
from tradebot.timing.nse import NseCalendar


def _delta(cfg: AppConfig, secrets: Secrets) -> DeltaClient:
    key = secrets.delta_api_key.get_secret_value() if secrets.delta_api_key else None
    sec = secrets.delta_api_secret.get_secret_value() if secrets.delta_api_secret else None
    return DeltaClient(cfg.crypto.base_url, key, sec, cfg.data.delta_rate_per_sec)


def _angel(cfg: AppConfig, secrets: Secrets):
    from tradebot.markets.angel.client import AngelClient
    creds = [secrets.angel_api_key, secrets.angel_client_code, secrets.angel_pin,
             secrets.angel_totp_secret]
    if not all(creds):
        raise SystemExit("NSE data needs ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PIN, "
                         "ANGEL_TOTP_SECRET in .env")
    d = cfg.data
    return AngelClient(*(c.get_secret_value() for c in creds),
                       AngelInstruments.load(d.angel_master_cache),
                       rate_per_sec=d.angel_rate_per_sec, local_ip=d.angel_client_local_ip,
                       public_ip=d.angel_client_public_ip, mac=d.angel_mac_address)


def cmd_backfill(args, cfg, secrets) -> int:
    market = Market.CRYPTO_DELTA if args.market == "crypto" else Market.NSE
    if market is Market.NSE and not cfg.equity.enabled:
        raise SystemExit("NSE is disabled (equity.enabled: false in config)")
    source = _delta(cfg, secrets) if market is Market.CRYPTO_DELTA else _angel(cfg, secrets)
    symbols = args.symbols.split(",") if args.symbols else (
        cfg.crypto.symbols if market is Market.CRYPTO_DELTA else cfg.equity.watchlist + ["INDIA VIX"])
    if market is Market.CRYPTO_DELTA and not args.symbols:
        symbols = [p + s for s in symbols for p in ("", "MARK:", "FUNDING:", "OI:")]
    store = MarketStore(cfg.data.db_path)
    ing = Ingestor(store, NseCalendar.from_yaml(cfg.timing.nse.holidays_file))
    for res in args.res.split(","):
        days = args.days or cfg.data.lookback_days.get(res, 30)
        for sym in symbols:
            print(ing.backfill(source, sym, res, days).line())
    store.close()
    return 0


def cmd_status(args, cfg, secrets) -> int:
    store = MarketStore(cfg.data.db_path)
    summary = store.summary()
    if summary.empty:
        print("store is empty: run `tradebot data backfill` first")
        return 0
    nse = NseCalendar.from_yaml(cfg.timing.nse.holidays_file)
    end = now_utc()
    for row in summary.itertuples():
        market = Market(row.market)
        gaps_txt = ""
        if ":" not in row.symbol:
            step = timedelta(seconds=RESOLUTION_SECONDS[row.resolution])
            df = store.candles(market, row.symbol, row.resolution)
            grid = expected_grid(market, row.resolution, row.first, end - step, nse)
            gaps = find_gaps(df["ts"], grid)
            gaps_txt = f"gaps {len(gaps)} ({sum(g.bars for g in gaps)} bars)"
        print(f"{row.market:<13} {row.symbol:<16} {row.resolution:<4} {row.bars:>7} bars  "
              f"{row.first.astimezone(IST):%Y-%m-%d %H:%M} -> "
              f"{row.last.astimezone(IST):%Y-%m-%d %H:%M} IST  {gaps_txt}")
    store.close()
    return 0


def cmd_verify(args, cfg, secrets) -> int:
    results = []
    delta = _delta(cfg, secrets)
    clock = CryptoClock(cfg.timing.crypto)
    for sym in cfg.crypto.symbols:
        results.append(check_funding_schedule(delta, clock, sym))
    inst = None
    if cfg.equity.enabled:
        inst = AngelInstruments.load(cfg.data.angel_master_cache)
        cal = NseCalendar.from_yaml(cfg.timing.nse.holidays_file)
        today = now_utc().astimezone(IST).date()
        rules = cfg.timing.nse.expiries
        for u in ("NIFTY", "BANKNIFTY"):
            results.append(check_expiries(cal, rules, inst, u, today))
        results.append(check_expiries(cal, [r.model_copy(update={"underlying": "RELIANCE"})
                                            for r in rules if r.underlying == "STOCKS"],
                                      inst, "RELIANCE", today, "FUTSTK"))
    for r in results:
        print(f"[{'OK ' if r.ok else 'FAIL'}] {r.name}: {r.detail}")
    if inst:
        for u in ("NIFTY", "BANKNIFTY"):
            print(f"[info] {u} lot size: {inst.lot_size(u)}")
    else:
        print("[skip] NSE expiry checks: equity disabled in config")
    return 0 if all(r.ok for r in results) else 1


def cmd_snapshot(args, cfg, secrets) -> int:
    delta = _delta(cfg, secrets)
    store = MarketStore(cfg.data.db_path)
    for sym in cfg.crypto.symbols:
        t = delta.ticker(sym)
        store.add_snapshot(Market.CRYPTO_DELTA, sym, "ticker", t)
        store.add_snapshot(Market.CRYPTO_DELTA, sym, "orderbook", delta.orderbook(sym, 20))
        print(f"{sym}: mark {float(t['mark_price']):,.1f}  funding {float(t['funding_rate']):.4f}%  "
              f"OI {t['oi']}  bid/ask {t['quotes']['best_bid']}/{t['quotes']['best_ask']}")
    store.close()
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("data", help="market data pipeline")
    dsub = p.add_subparsers(dest="data_cmd", required=True)
    b = dsub.add_parser("backfill", help="incremental fetch into the local store")
    b.add_argument("--market", choices=["crypto", "nse"], default="crypto")
    b.add_argument("--symbols", help="comma separated; default = config watchlist")
    b.add_argument("--res", default="1h", help="comma separated, e.g. 5m,1h,1d")
    b.add_argument("--days", type=int, help="lookback; default from data.lookback_days")
    b.set_defaults(data_fn=cmd_backfill)
    dsub.add_parser("status", help="stored coverage + gaps").set_defaults(data_fn=cmd_status)
    dsub.add_parser("verify", help="check funding times + expiry rules against exchanges"
                    ).set_defaults(data_fn=cmd_verify)
    dsub.add_parser("snapshot", help="store ticker + orderbook snapshot").set_defaults(
        data_fn=cmd_snapshot)
    p.set_defaults(fn=lambda args: args.data_fn(args, *load_config(args.config_dir, args.env_file)))
