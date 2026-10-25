import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd
import pytest

from tradebot.config import ExpiryRule
from tradebot.core.models import Market
from tradebot.data.http import ApiClient, HttpError
from tradebot.data.ingest import Ingestor
from tradebot.data.quality import expected_grid, find_gaps, ohlc_issues
from tradebot.data.store import MarketStore
from tradebot.data.verify import check_expiries
from tradebot.markets.angel.client import AngelClient
from tradebot.markets.angel.instruments import AngelInstruments
from tradebot.markets.delta.client import DeltaClient, sign
from tradebot.timeutil import IST
from tradebot.timing.nse import NseCalendar

ROOT = Path(__file__).resolve().parents[1]
NSE = NseCalendar.from_yaml(ROOT / "config/calendars/nse_holidays.yaml")
T0 = datetime(2026, 10, 16, 0, tzinfo=UTC)


def no_sleep(_):
    pass


# ---------- http ----------

def test_retries_429_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    api = ApiClient("https://x", 100, transport=httpx.MockTransport(handler), sleep=no_sleep)
    assert api.request("GET", "/") == {"ok": True} and len(calls) == 3


def test_does_not_retry_400():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, text="bad order")

    api = ApiClient("https://x", 100, transport=httpx.MockTransport(handler), sleep=no_sleep)
    with pytest.raises(HttpError):
        api.request("POST", "/orders")
    assert len(calls) == 1


# ---------- delta ----------

def delta_candles_handler(req: httpx.Request):
    q = dict(req.url.params)
    s, e = int(q["start"]), int(q["end"])
    rows = [{"time": t, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}
            for t in range(s - s % 3600, e, 3600) if t != int((T0 + timedelta(hours=5)).timestamp())]
    return httpx.Response(200, json={"success": True, "result": rows[::-1]})  # newest first


def test_delta_candles_chunked_sorted_and_bounded():
    delta = DeltaClient("https://x", transport=httpx.MockTransport(delta_candles_handler),
                        sleep=no_sleep)
    delta.MAX_BARS = 10  # force several requests
    df = delta.candles("BTCUSD", "1h", T0, T0 + timedelta(hours=48))
    assert len(df) == 47 and df["ts"].is_monotonic_increasing
    assert df["ts"].iloc[0] == pd.Timestamp(T0) and df["ts"].iloc[-1] < pd.Timestamp(T0 + timedelta(hours=48))


def test_delta_signature_matches_spec():
    # method + timestamp + path + query + body
    import hashlib
    import hmac
    expected = hmac.new(b"s3cret", b"GET1700000000/v2/orders?state=open", hashlib.sha256).hexdigest()
    assert sign("s3cret", "GET", "1700000000", "/v2/orders", "?state=open", "") == expected


# ---------- store + ingest + gaps ----------

def test_ingest_is_incremental_and_finds_gap(tmp_path):
    store = MarketStore(tmp_path / "m.duckdb")
    delta = DeltaClient("https://x", transport=httpx.MockTransport(delta_candles_handler),
                        sleep=no_sleep)
    ing = Ingestor(store, NSE)
    end = T0 + timedelta(hours=24, minutes=30)
    rep = ing.backfill(delta, "BTCUSD", "1h", lookback_days=1, end=end)
    assert rep.rows == 23                      # 24 closed bars minus the missing 05:00 bar
    assert [(g.start, g.bars) for g in rep.gaps] == [(pd.Timestamp(T0 + timedelta(hours=5)), 1)]
    rep2 = ing.backfill(delta, "BTCUSD", "1h", lookback_days=1, end=end)
    assert rep2.rows == 1                      # only the last stored bar is re-fetched
    assert len(store.candles(Market.CRYPTO_DELTA, "BTCUSD", "1h")) == 23


def test_forming_bar_not_stored(tmp_path):
    store = MarketStore(tmp_path / "m.duckdb")
    delta = DeltaClient("https://x", transport=httpx.MockTransport(delta_candles_handler),
                        sleep=no_sleep)
    Ingestor(store, NSE).backfill(delta, "BTCUSD", "1h", 1, end=T0 + timedelta(hours=2, minutes=10))
    last = store.last_ts(Market.CRYPTO_DELTA, "BTCUSD", "1h")
    assert pd.Timestamp(last) == pd.Timestamp(T0 + timedelta(hours=1))   # 02:00 bar still forming


def test_upsert_replaces_bar(tmp_path):
    store = MarketStore(tmp_path / "m.duckdb")
    bar = {"ts": [T0], "open": [1.0], "high": [2.0], "low": [0.5], "close": [1.0], "volume": [1.0]}
    store.upsert_candles(Market.CRYPTO_DELTA, "X", "1h", pd.DataFrame(bar))
    store.upsert_candles(Market.CRYPTO_DELTA, "X", "1h", pd.DataFrame(bar | {"close": [1.9]}))
    df = store.candles(Market.CRYPTO_DELTA, "X", "1h")
    assert len(df) == 1 and df["close"].iloc[0] == 1.9


def test_nse_grid_skips_holidays_and_weekends():
    start = datetime(2026, 10, 16, tzinfo=IST)            # Fri
    end = datetime(2026, 10, 22, tzinfo=IST)              # Thu (Tue 20 = Dussehra)
    grid = expected_grid(Market.NSE, "1h", start, end, NSE)
    days = sorted({t.tz_convert(IST).date() for t in grid})
    assert days == [date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 21)]
    first = grid[0].tz_convert(IST)
    assert (first.hour, first.minute) == (9, 15) and len(grid) == 3 * 7   # 09:15..15:15


def test_find_gaps_groups_runs():
    grid = pd.date_range(T0, periods=10, freq="1h")
    have = pd.Series(grid.delete([2, 3, 7]))
    gaps = find_gaps(have, grid)
    assert [(g.start.hour, g.bars) for g in gaps] == [(2, 2), (7, 1)]


def test_ohlc_issues():
    df = pd.DataFrame({"ts": [T0, T0], "open": [1, 5], "high": [2, 4], "low": [0.5, 3],
                       "close": [1, 3.5], "volume": [1, -1]})
    issues = " | ".join(ohlc_issues(df))
    assert "duplicate" in issues and "violate" in issues and "negative volume" in issues


# ---------- angel ----------

MASTER = [
    {"token": "99926000", "symbol": "Nifty 50", "name": "NIFTY", "expiry": "", "lotsize": "1",
     "instrumenttype": "AMXIDX", "exch_seg": "NSE"},
    {"token": "2885", "symbol": "RELIANCE-EQ", "name": "RELIANCE", "expiry": "", "lotsize": "1",
     "instrumenttype": "", "exch_seg": "NSE"},
    *[{"token": str(i), "symbol": f"NIFTY{e}", "name": "NIFTY", "expiry": e, "lotsize": "65",
       "instrumenttype": "OPTIDX", "exch_seg": "NFO"}
      for i, e in enumerate(["19OCT2026", "27OCT2026", "03NOV2026", "09NOV2026"])],
    {"token": "9", "symbol": "NIFTY27OCT26FUT", "name": "NIFTY", "expiry": "27OCT2026",
     "lotsize": "65", "instrumenttype": "FUTIDX", "exch_seg": "NFO"},
]


def test_instruments_tokens_and_lots():
    inst = AngelInstruments(MASTER)
    assert inst.token("NIFTY") == ("NSE", "99926000")
    assert inst.token("RELIANCE") == ("NSE", "2885")
    assert inst.lot_size("NIFTY") == 65
    with pytest.raises(KeyError):
        inst.token("NOPE")


def test_expiry_check_passes_and_detects_rule_change():
    inst = AngelInstruments(MASTER)
    good = [ExpiryRule(underlying="NIFTY", cycle="weekly", weekday="tue"),
            ExpiryRule(underlying="NIFTY", cycle="monthly", weekday="tue")]
    assert check_expiries(NSE, good, inst, "NIFTY", date(2026, 10, 16), horizon_days=21).ok
    stale = [ExpiryRule(underlying="NIFTY", cycle="weekly", weekday="thu")]  # the old rule
    res = check_expiries(NSE, stale, inst, "NIFTY", date(2026, 10, 16), horizon_days=21)
    assert not res.ok and "exchange-only" in res.detail


def test_angel_relogins_on_expired_token_and_parses_candles():
    logins, seen = [], []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        if req.url.path.endswith("loginByPassword"):
            logins.append(body)
            return httpx.Response(200, json={"status": True, "data": {"jwtToken": f"jwt{len(logins)}"}})
        seen.append(req.headers["Authorization"])
        if len(seen) == 1:
            return httpx.Response(200, json={"status": False, "errorcode": "AG8001",
                                             "message": "Invalid Token"})
        return httpx.Response(200, json={"status": True, "data": [
            ["2026-10-16T09:15:00+05:30", 100, 101, 99, 100.5, 1000],
            ["2026-10-16T10:15:00+05:30", 100.5, 102, 100, 101, 900]]})

    c = AngelClient("k", "C123", "1111", "JBSWY3DPEHPK3PXP", AngelInstruments(MASTER),
                    transport=httpx.MockTransport(handler), sleep=no_sleep)
    df = c.candles("NIFTY", "1h", datetime(2026, 10, 16, 9, 15, tzinfo=IST),
                   datetime(2026, 10, 16, 15, 30, tzinfo=IST))
    assert len(logins) == 2 and seen == ["Bearer jwt1", "Bearer jwt2"]
    assert len(df) == 2 and df["ts"].iloc[0] == pd.Timestamp("2026-10-16T03:45:00Z")
    assert len(logins[0]["totp"]) == 6


def test_backfill_fills_older_history_backwards(tmp_path):
    store = MarketStore(tmp_path / "m.duckdb")
    delta = DeltaClient("https://x", transport=httpx.MockTransport(delta_candles_handler),
                        sleep=no_sleep)
    ing = Ingestor(store, NSE)
    end = T0 + timedelta(hours=48, minutes=30)
    ing.backfill(delta, "BTCUSD", "1h", lookback_days=1, end=end)          # last 24h only
    rep = ing.backfill(delta, "BTCUSD", "1h", lookback_days=2, end=end)    # now ask for 48h
    df = store.candles(Market.CRYPTO_DELTA, "BTCUSD", "1h")
    assert df["ts"].iloc[0] == pd.Timestamp(T0) and len(df) == 47          # 48 minus missing 05:00
    assert [(g.start, g.bars) for g in rep.gaps] == [(pd.Timestamp(T0 + timedelta(hours=5)), 1)]
