"""Angel One SmartAPI REST client (data side). Routes/headers match the official smartapi-python SDK.

Login is TOTP-based and fully automatic (no daily manual step). Sessions expire daily;
token errors trigger one re-login + retry.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pandas as pd
import pyotp

from tradebot.core.interfaces import MarketData
from tradebot.core.models import Market
from tradebot.data.http import ApiClient
from tradebot.markets.angel.instruments import AngelInstruments
from tradebot.timeutil import IST

BASE_URL = "https://apiconnect.angelone.in"
LOGIN = "/rest/auth/angelbroking/user/v1/loginByPassword"
CANDLES = "/rest/secure/angelbroking/historical/v1/getCandleData"
QUOTE = "/rest/secure/angelbroking/market/v1/quote/"

# resolution -> (Angel interval, max days per request)
INTERVALS = {
    "1m": ("ONE_MINUTE", 30), "3m": ("THREE_MINUTE", 60), "5m": ("FIVE_MINUTE", 100),
    "10m": ("TEN_MINUTE", 100), "15m": ("FIFTEEN_MINUTE", 200), "30m": ("THIRTY_MINUTE", 200),
    "1h": ("ONE_HOUR", 400), "1d": ("ONE_DAY", 2000),
}
TOKEN_ERRORS = {"AG8001", "AG8002", "AG8003", "AB1010"}  # invalid/expired session


class AngelError(RuntimeError):
    def __init__(self, code: str | None, message: str):
        self.code = code
        super().__init__(f"Angel {code}: {message}")


class AngelClient(MarketData):
    market = Market.NSE

    def __init__(self, api_key: str, client_code: str, pin: str, totp_secret: str,
                 instruments: AngelInstruments, *, rate_per_sec: float = 3,
                 local_ip: str = "127.0.0.1", public_ip: str = "127.0.0.1",
                 mac: str = "00:00:00:00:00:00", base_url: str = BASE_URL,
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self.api = ApiClient(base_url, rate_per_sec, transport=transport, sleep=sleep, headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "X-UserType": "USER", "X-SourceID": "WEB", "X-PrivateKey": api_key,
            "X-ClientLocalIP": local_ip, "X-ClientPublicIP": public_ip, "X-MACAddress": mac})
        self._client_code, self._pin, self._totp = client_code, pin, totp_secret
        self._jwt: str | None = None
        self.instruments = instruments

    # ---------- session ----------

    def login(self) -> None:
        data = self.api.request("POST", LOGIN, json={
            "clientcode": self._client_code, "password": self._pin,
            "totp": pyotp.TOTP(self._totp).now()})
        if not data.get("status"):
            raise AngelError(data.get("errorcode"), data.get("message", "login failed"))
        self._jwt = data["data"]["jwtToken"]

    def _post(self, path: str, body: dict) -> dict | list | None:
        for attempt in (0, 1):
            if not self._jwt:
                self.login()
            data = self.api.request("POST", path, json=body,
                                    headers={"Authorization": f"Bearer {self._jwt}"})
            if data.get("status"):
                return data.get("data")
            if data.get("errorcode") in TOKEN_ERRORS and attempt == 0:
                self._jwt = None
                continue
            raise AngelError(data.get("errorcode"), data.get("message", ""))
        raise AssertionError("unreachable")

    # ---------- market data ----------

    def candles(self, symbol: str, resolution: str, start: datetime, end: datetime) -> pd.DataFrame:
        """symbol: NIFTY, BANKNIFTY, INDIA VIX or an NSE stock (RELIANCE)."""
        exchange, token = self.instruments.token(symbol)
        interval, max_days = INTERVALS[resolution]
        frames, s = [], start.astimezone(IST)
        end_ist = end.astimezone(IST)
        while s < end_ist:
            e = min(s + timedelta(days=max_days), end_ist)
            rows = self._post(CANDLES, {
                "exchange": exchange, "symboltoken": token, "interval": interval,
                "fromdate": s.strftime("%Y-%m-%d %H:%M"), "todate": e.strftime("%Y-%m-%d %H:%M")})
            if rows:
                frames.append(pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]))
            s = e
        if not frames:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
        df = pd.concat(frames, ignore_index=True)
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df.drop_duplicates("ts").sort_values("ts")
        return df[(df["ts"] >= pd.Timestamp(start)) & (df["ts"] < pd.Timestamp(end))].reset_index(drop=True)

    def quote(self, symbols: list[str], mode: str = "FULL") -> list[dict]:
        by_exch: dict[str, list[str]] = {}
        for sym in symbols:
            exch, token = self.instruments.token(sym)
            by_exch.setdefault(exch, []).append(token)
        data = self._post(QUOTE, {"mode": mode, "exchangeTokens": by_exch}) or {}
        return data.get("fetched", [])

    def last_price(self, symbol: str) -> Decimal:
        rows = self.quote([symbol], mode="LTP")
        if not rows:
            raise AngelError(None, f"no quote for {symbol}")
        return Decimal(str(rows[0]["ltp"]))
