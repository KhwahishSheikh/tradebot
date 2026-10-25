"""Delta Exchange REST client (market data now; order endpoints use the same signer in Ticket 9)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

import httpx
import pandas as pd

from tradebot.core.interfaces import MarketData
from tradebot.core.models import Market
from tradebot.data.http import ApiClient
from tradebot.data.quality import RESOLUTION_SECONDS


class DeltaError(RuntimeError):
    pass


def sign(secret: str, method: str, timestamp: str, path: str, query: str, body: str) -> str:
    """HMAC-SHA256(secret, method + timestamp + path + query_string + body). query includes '?'."""
    msg = method + timestamp + path + query + body
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


class DeltaClient(MarketData):
    market = Market.CRYPTO_DELTA
    MAX_BARS = 2000  # API returns up to ~4000; stay well under

    def __init__(self, base_url: str, api_key: str | None = None, api_secret: str | None = None,
                 rate_per_sec: float = 5, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep):
        self.api = ApiClient(base_url, rate_per_sec, transport=transport, sleep=sleep)
        self._key, self._secret = api_key, api_secret

    # ---------- transport ----------

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
              auth: bool = False, retries: int | None = None):
        query = "?" + urlencode(params) if params else ""
        payload = json.dumps(body, separators=(",", ":")) if body else ""
        headers = {"Content-Type": "application/json"}
        if auth:
            if not (self._key and self._secret):
                raise DeltaError("DELTA_API_KEY / DELTA_API_SECRET required")
            ts = str(int(time.time()))
            headers |= {"api-key": self._key, "timestamp": ts,
                        "signature": sign(self._secret, method, ts, path, query, payload)}
        data = self.api.request(method, path + query, headers=headers, content=payload or None,
                                retries=retries)
        if not data.get("success", False):
            raise DeltaError(f"{path}: {data.get('error')}")
        return data["result"]

    # ---------- market data ----------

    def candles(self, symbol: str, resolution: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Bars with open time in [start, end). symbol may be BTCUSD, MARK:BTCUSD, FUNDING:BTCUSD, OI:BTCUSD."""
        step = RESOLUTION_SECONDS[resolution]
        chunk = timedelta(seconds=step * self.MAX_BARS)
        frames, s = [], start
        while s < end:
            e = min(s + chunk, end)
            rows = self._call("GET", "/v2/history/candles", {
                "resolution": resolution, "symbol": symbol,
                "start": int(s.timestamp()), "end": int(e.timestamp())})
            if rows:
                frames.append(pd.DataFrame(rows))
            s = e
        if not frames:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
        df = pd.concat(frames, ignore_index=True)
        df["ts"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.drop(columns="time").drop_duplicates("ts").sort_values("ts")
        df = df[(df["ts"] >= pd.Timestamp(start)) & (df["ts"] < pd.Timestamp(end))]
        return df[["ts", "open", "high", "low", "close", "volume"]].reset_index(drop=True)

    def ticker(self, symbol: str) -> dict:
        return self._call("GET", f"/v2/tickers/{symbol}")

    def orderbook(self, symbol: str, depth: int = 20) -> dict:
        return self._call("GET", f"/v2/l2orderbook/{symbol}", {"depth": depth})

    def product(self, symbol: str) -> dict:
        return self._call("GET", f"/v2/products/{symbol}")

    def last_price(self, symbol: str) -> Decimal:
        return Decimal(str(self.ticker(symbol)["mark_price"]))

    # ---------- checks ----------

    def funding_change_hours(self, symbol: str, days: int = 7) -> list[datetime]:
        """UTC hours at which the funding rate changed: these are Delta's funding timestamps."""
        end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        df = self.candles(f"FUNDING:{symbol}", "1h", end - timedelta(days=days), end)
        changed = df["close"].diff().fillna(0) != 0
        return [t.to_pydatetime() for t in df.loc[changed, "ts"]]
