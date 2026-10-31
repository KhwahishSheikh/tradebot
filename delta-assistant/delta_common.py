"""Shared helpers for the delta-assistant tools: config, Delta REST client, indicators, time.

Python 3.9+, only `requests` (+ optional `python-dotenv`). Keys are read from .env and never printed.
"""
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
RULES_FILE = HERE / "rules.json"
RES_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

try:
    from zoneinfo import ZoneInfo
    UK = ZoneInfo("Europe/London")
except Exception:  # pragma: no cover - very old Python
    UK = timezone.utc


# ---------------------------------------------------------------- config

def load_env():
    """Read delta-assistant/.env into os.environ (without overriding real env vars)."""
    path = HERE / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def is_live():
    load_env()
    return os.environ.get("LIVE", "false").strip().lower() == "true"


def base_url():
    load_env()
    if is_live():
        return os.environ.get("DELTA_LIVE_URL") or "https://api.india.delta.exchange"
    return os.environ.get("DELTA_TESTNET_URL") or "https://cdn-ind.testnet.deltaex.org"


PUBLIC_URL = "https://api.india.delta.exchange"  # market data always from live (public, no keys)


def load_rules():
    return json.loads(RULES_FILE.read_text())


# ---------------------------------------------------------------- REST client

class DeltaError(RuntimeError):
    pass


def sign(secret, method, timestamp, path, query, body):
    """HMAC-SHA256(secret, method + timestamp + path + query_string + body). query includes '?'."""
    return hmac.new(secret.encode(), (method + timestamp + path + query + body).encode(),
                    hashlib.sha256).hexdigest()


class Delta:
    def __init__(self, base=None, auth=False):
        load_env()
        self.base = (base or (base_url() if auth else PUBLIC_URL)).rstrip("/")
        self.key = os.environ.get("DELTA_API_KEY", "")
        self.secret = os.environ.get("DELTA_API_SECRET", "")
        self.auth = auth
        if auth and not (self.key and self.secret):
            raise DeltaError("DELTA_API_KEY / DELTA_API_SECRET missing in delta-assistant/.env")

    def call(self, method, path, params=None, body=None, auth=None):
        auth = self.auth if auth is None else auth
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        payload = json.dumps(body, separators=(",", ":")) if body else ""
        headers = {"Content-Type": "application/json", "User-Agent": "delta-assistant/1.0"}
        if auth:
            ts = str(int(time.time()))
            headers.update({"api-key": self.key, "timestamp": ts,
                            "signature": sign(self.secret, method, ts, path, query, payload)})
        r = requests.request(method, self.base + path + query, headers=headers,
                             data=payload or None, timeout=15)
        try:
            data = r.json()
        except ValueError:
            raise DeltaError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
        if not data.get("success", False):
            raise DeltaError(f"{method} {path}: {data.get('error') or r.status_code}")
        return data["result"]

    # public
    def ticker(self, sym):
        return self.call("GET", f"/v2/tickers/{sym}", auth=False)

    def product(self, sym):
        return self.call("GET", f"/v2/products/{sym}", auth=False)

    def candles(self, sym, res, bars=400, end=None):
        end = int(end or time.time())
        step = RES_SECONDS[res]
        out, start = {}, end - step * bars
        while start < end:  # API caps rows per call: page in chunks of 1500
            chunk_end = min(end, start + step * 1500)
            for c in self.call("GET", "/v2/history/candles", {
                    "resolution": res, "symbol": sym, "start": start, "end": chunk_end}, auth=False):
                out[c["time"]] = c
            start = chunk_end
        rows = sorted(out.values(), key=lambda c: c["time"])
        return [c for c in rows if c["time"] + step <= time.time()]  # closed bars only

    # private
    def wallet_usd(self):
        for b in self.call("GET", "/v2/wallet/balances"):
            if b.get("asset_symbol") == "USD":
                return b
        return {}

    def positions(self):
        return [p for p in self.call("GET", "/v2/positions/margined") if int(p.get("size") or 0)]

    def open_orders(self, product_id=None):
        return self.call("GET", "/v2/orders", {"product_ids": product_id, "states": "open,pending"})

    def fills_since(self, since_utc):
        rows = self.call("GET", "/v2/fills", {"page_size": 100})
        return [f for f in rows if parse_ts(f["created_at"]) >= since_utc]


# ---------------------------------------------------------------- indicators

def ema(values, n):
    k, e = 2 / (n + 1), sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def ema_series(values, n):
    k, e, out = 2 / (n + 1), sum(values[:n]) / n, []
    for i, v in enumerate(values):
        if i >= n:
            e = v * k + e * (1 - k)
        out.append(e if i >= n - 1 else None)
    return out


def rsi(closes, n=14):
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gain, loss = gain + max(d, 0), loss + max(-d, 0)
    gain, loss = gain / n, loss / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain, loss = (gain * (n - 1) + max(d, 0)) / n, (loss * (n - 1) + max(-d, 0)) / n
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def atr(rows, n=14):
    tr = [max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
          for p, c in zip(rows, rows[1:])]
    a = sum(tr[:n]) / n
    for t in tr[n:]:
        a = (a * (n - 1) + t) / n
    return a


def swings(rows, k=3, lookback=120):
    rows = rows[-lookback:]
    highs = [rows[i]["high"] for i in range(k, len(rows) - k)
             if rows[i]["high"] == max(r["high"] for r in rows[i - k:i + k + 1])]
    lows = [rows[i]["low"] for i in range(k, len(rows) - k)
            if rows[i]["low"] == min(r["low"] for r in rows[i - k:i + k + 1])]
    return highs, lows


def trend(rows):
    closes = [c["close"] for c in rows]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    c = closes[-1]
    if c > e50 > e200 and e20 > e50:
        return "UP"
    if c < e50 < e200 and e20 < e50:
        return "DOWN"
    return "MIXED"


# ---------------------------------------------------------------- misc

def parse_ts(s):
    """Delta ISO time ('2026-10-16T20:07:09.307610Z', up to 9 fraction digits) -> aware UTC."""
    s = s.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def now_utc():
    return datetime.now(timezone.utc)


def uk_utc(dt=None):
    dt = dt or now_utc()
    return f"{dt.astimezone(UK):%a %H:%M} UK ({dt:%H:%M} UTC)"


def fmt(x, ref=None):
    ref = abs(ref if ref is not None else x)
    digits = 1 if ref >= 1000 else 2 if ref >= 10 else 4 if ref >= 0.1 else 6
    return f"{x:.{digits}f}"


def round_tick(price, tick):
    return round(round(price / tick) * tick, 10)


def liq_price(entry, side, leverage, mm_pct):
    """Isolated-style estimate: liquidation when loss eats initial margin minus maintenance margin.
    Cross margin can be further away (whole wallet backs it) — treat this as the worst case."""
    dist = max(1 / leverage - mm_pct / 100, 0)
    return entry * (1 - dist) if side == "buy" else entry * (1 + dist)
