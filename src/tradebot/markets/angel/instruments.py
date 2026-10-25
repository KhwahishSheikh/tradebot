"""Angel One instrument master (public JSON, ~150k rows, refreshed daily).

Source of truth for symbol tokens, lot sizes and the exchange's actual F&O expiry dates.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import httpx

MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# Our symbol -> (exchange, Angel trading symbol) for indices
INDEX_SYMBOLS = {
    "NIFTY": ("NSE", "Nifty 50"),
    "BANKNIFTY": ("NSE", "Nifty Bank"),
    "INDIA VIX": ("NSE", "India VIX"),
}


class AngelInstruments:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self._by_symbol = {(r["exch_seg"], r["symbol"]): r for r in rows}

    @classmethod
    def load(cls, cache: Path | str, max_age_hours: float = 20,
             transport: httpx.BaseTransport | None = None) -> AngelInstruments:
        cache = Path(cache)
        if not cache.exists() or time.time() - cache.stat().st_mtime > max_age_hours * 3600:
            cache.parent.mkdir(parents=True, exist_ok=True)
            with httpx.Client(timeout=120, transport=transport) as c:
                r = c.get(MASTER_URL)
                r.raise_for_status()
            tmp = cache.with_suffix(".tmp")
            tmp.write_bytes(r.content)
            tmp.replace(cache)  # atomic: never leave a half-written master
        return cls(json.loads(cache.read_text()))

    def token(self, symbol: str) -> tuple[str, str]:
        """(exchange, symboltoken) for NIFTY / BANKNIFTY / INDIA VIX / an NSE stock name."""
        exch, angel_sym = INDEX_SYMBOLS.get(symbol, ("NSE", f"{symbol}-EQ"))
        row = self._by_symbol.get((exch, angel_sym))
        if not row:
            raise KeyError(f"{symbol} ({exch}:{angel_sym}) not in Angel instrument master")
        return exch, row["token"]

    def lot_size(self, underlying: str) -> int:
        for r in self.rows:
            if r["name"] == underlying and r["exch_seg"] == "NFO" and r["instrumenttype"] in (
                    "FUTIDX", "FUTSTK"):
                return int(r["lotsize"])
        raise KeyError(f"no futures listed for {underlying}")

    def expiries(self, underlying: str, instrument_type: str = "OPTIDX") -> list[date]:
        """Expiry dates the exchange actually lists for this underlying."""
        out = {datetime.strptime(r["expiry"], "%d%b%Y").date() for r in self.rows
               if r["name"] == underlying and r["exch_seg"] == "NFO"
               and r["instrumenttype"] == instrument_type and r["expiry"]}
        return sorted(out)

    def expiries_by_underlying(self) -> dict[str, list[date]]:
        acc: dict[str, set[date]] = defaultdict(set)
        for r in self.rows:
            if r["exch_seg"] == "NFO" and r["expiry"]:
                acc[r["name"]].add(datetime.strptime(r["expiry"], "%d%b%Y").date())
        return {k: sorted(v) for k, v in acc.items()}
