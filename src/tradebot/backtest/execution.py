"""Trade execution simulator on 5-minute bars (independent of portfolio sizing).

For a signal produced at bar close T:
  1. approval arrives at T + delay; fill at the OPEN of the first 5m bar starting at/after it
  2. drift check vs the signal's entry (same rule as the live risk engine) -> else skipped
  3. entry = that open +/- slippage, taker fee (+GST)
  4. stop / target / max-hold walk on 5m bars; stop wins if both are inside one bar;
     gaps through the stop fill at the open; target is a resting limit (maker fee, no slippage)
  5. funding settles at each funding timestamp the position is open across
All money amounts are PER 1 UNIT of the underlying (1 BTC / 1 ETH); the portfolio multiplies by size.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from tradebot.config import CryptoCosts
from tradebot.core.models import Market, Side
from tradebot.data.store import MarketStore

FIVE_MIN = np.timedelta64(5, "m")


@dataclass
class ExecData:
    symbol: str
    ts: np.ndarray            # datetime64[ns] bar opens (UTC, naive)
    o: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    funding_ts: np.ndarray    # datetime64[ns] settlement times
    funding_rate: np.ndarray  # % per 8h settled at that time

    @classmethod
    def load(cls, store: MarketStore, symbol: str, start=None, end=None) -> ExecData:
        b = store.candles(Market.CRYPTO_DELTA, symbol, "5m", start, end)
        if b.empty:
            raise LookupError(f"no 5m bars for {symbol}: run `tradebot data backfill --res 5m`")
        f = store.candles(Market.CRYPTO_DELTA, f"FUNDING:{symbol}", "1h", start, end)
        # The hourly FUNDING series shows the rate for the running 8h interval; it settles at the
        # interval end. Settlement at S uses the value shown in the hour before S.
        f["settle"] = f["ts"] + pd.Timedelta(hours=1)
        f = f[f["settle"].dt.hour.isin([0, 8, 16]) & (f["settle"].dt.minute == 0)]
        to_np = lambda s: s.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy("datetime64[ns]")
        return cls(symbol, to_np(b["ts"]), *(b[k].to_numpy(float) for k in ("open", "high", "low", "close")),
                   to_np(f["settle"]), f["close"].to_numpy(float))

    def index_at_or_after(self, t: np.datetime64) -> int:
        return int(np.searchsorted(self.ts, t, side="left"))


@dataclass
class TradeSim:
    status: str                    # filled | skipped
    reason: str                    # exit reason, or skip reason
    entry_ts: datetime | None = None
    entry_px: float = 0.0
    ref_px: float = 0.0            # price the risk engine sees at execution (pre-slippage)
    exit_ts: datetime | None = None
    exit_px: float = 0.0
    gross_pu: float = 0.0          # price PnL per unit
    fees_pu: float = 0.0
    funding_pu: float = 0.0        # +ve = paid
    risk_pu: float = 0.0           # |entry - stop| per unit

    @property
    def net_pu(self) -> float:
        return self.gross_pu - self.fees_pu - self.funding_pu

    @property
    def net_r(self) -> float:
        return self.net_pu / self.risk_pu if self.risk_pu else 0.0


def _np_utc(t: datetime) -> np.datetime64:
    return np.datetime64(pd.Timestamp(t).tz_convert("UTC").tz_localize(None), "ns")


def _py(t: np.datetime64) -> datetime:
    return pd.Timestamp(t).tz_localize("UTC").to_pydatetime()


def simulate_trade(ex: ExecData, side: Side, signal_close: datetime, entry_ref: float,
                   stop: float, target: float, max_hold: timedelta, costs: CryptoCosts,
                   delay: timedelta, max_drift_pct: float, trail_dist: float = 0.0,
                   trail_activate_r: float = 1.0) -> TradeSim:
    j = ex.index_at_or_after(_np_utc(signal_close + delay))
    if j >= len(ex.ts):
        return TradeSim("skipped", "no data after approval")
    if ex.ts[j] - _np_utc(signal_close + delay) > np.timedelta64(30, "m"):
        return TradeSim("skipped", "data gap at approval time")
    long_ = side is Side.BUY
    sgn = 1 if long_ else -1
    ref = ex.o[j]
    if abs(ref - entry_ref) / entry_ref * 100 > max_drift_pct:
        return TradeSim("skipped", "price drift", ref_px=ref)
    if (long_ and ref <= stop) or (not long_ and ref >= stop):
        return TradeSim("skipped", "beyond stop at approval", ref_px=ref)

    slip = costs.slippage_pct / 100
    taker = costs.taker_fee_pct / 100 * (1 + costs.gst_pct / 100)
    maker = costs.maker_fee_pct / 100 * (1 + costs.gst_pct / 100)
    fill = ref * (1 + sgn * slip)

    end_t = ex.ts[j] + np.timedelta64(int(max_hold.total_seconds()), "s")
    last = min(int(np.searchsorted(ex.ts, end_t, side="left")), len(ex.ts)) - 1
    last = max(last, j)
    if trail_dist > 0:
        return _simulate_trailing(ex, j, last, long_, fill, ref, stop, target, slip, taker, maker,
                                  trail_dist, trail_activate_r)
    w = slice(j, last + 1)
    stop_hit = (ex.lo[w] <= stop) if long_ else (ex.h[w] >= stop)
    tgt_hit = (ex.h[w] >= target) if long_ else (ex.lo[w] <= target)
    ks = np.flatnonzero(stop_hit)
    kt = np.flatnonzero(tgt_hit)
    k_stop = ks[0] if ks.size else None
    k_tgt = kt[0] if kt.size else None

    if k_stop is not None and (k_tgt is None or k_stop <= k_tgt):      # stop first (pessimistic)
        k = j + k_stop
        gapped = k > j and ((ex.o[k] <= stop) if long_ else (ex.o[k] >= stop))
        px = (ex.o[k] if gapped else stop) * (1 - sgn * slip)
        why, fee_out = ("stop(gap)" if gapped else "stop"), taker
    elif k_tgt is not None:
        k = j + k_tgt
        gapped = k > j and ((ex.o[k] >= target) if long_ else (ex.o[k] <= target))
        px, why, fee_out = (ex.o[k] if gapped else target), "target", maker
    else:
        k = last
        px = ex.c[k] * (1 - sgn * slip)
        why = "time" if last + 1 < len(ex.ts) else "open_at_data_end"
        fee_out = taker

    return _finish(ex, j, k, long_, fill, ref, px, why, taker, fee_out, stop)


def _finish(ex: ExecData, j: int, k: int, long_: bool, fill: float, ref: float, px: float,
            why: str, taker: float, fee_out: float, initial_stop: float) -> TradeSim:
    sgn = 1 if long_ else -1
    exit_t = ex.ts[k] + FIVE_MIN
    fm = (ex.funding_ts > ex.ts[j]) & (ex.funding_ts <= exit_t)
    funding = 0.0
    if fm.any():
        idx = np.clip(np.searchsorted(ex.ts, ex.funding_ts[fm]) - 1, 0, len(ex.ts) - 1)
        funding = float(np.sum(sgn * ex.funding_rate[fm] / 100 * ex.c[idx]))

    return TradeSim("filled", why, _py(ex.ts[j]), fill, ref, _py(exit_t), px,
                    gross_pu=sgn * (px - fill), fees_pu=fill * taker + px * fee_out,
                    funding_pu=funding, risk_pu=abs(fill - initial_stop))


def _simulate_trailing(ex: ExecData, j: int, last: int, long_: bool, fill: float, ref: float,
                       stop: float, target: float, slip: float, taker: float, maker: float,
                       trail_dist: float, activate_r: float) -> TradeSim:
    """Same rules as the live position manager: once the trade is >= activate_r R in profit at a
    bar close, ratchet the stop to close -/+ trail_dist (never loosens). Checked bar by bar."""
    sgn = 1 if long_ else -1
    initial, cur = stop, stop
    risk = abs(fill - stop)
    active = False
    for k in range(j, last + 1):
        o, h, lo, c = ex.o[k], ex.h[k], ex.lo[k], ex.c[k]
        hit_stop = lo <= cur if long_ else h >= cur
        if hit_stop:
            gapped = k > j and ((o <= cur) if long_ else (o >= cur))
            px = (o if gapped else cur) * (1 - sgn * slip)
            why = ("trail_stop" if active else "stop") + ("(gap)" if gapped else "")
            return _finish(ex, j, k, long_, fill, ref, px, why, taker, taker, initial)
        if (h >= target) if long_ else (lo <= target):
            px = (o if k > j and ((o >= target) if long_ else (o <= target)) else target)
            return _finish(ex, j, k, long_, fill, ref, px, "target", taker, maker, initial)
        if not active and sgn * (c - fill) >= activate_r * risk:
            active = True
        if active:
            cand = c - sgn * trail_dist
            cur = max(cur, cand) if long_ else min(cur, cand)
    px = ex.c[last] * (1 - sgn * slip)
    why = "time" if last + 1 < len(ex.ts) else "open_at_data_end"
    return _finish(ex, j, last, long_, fill, ref, px, why, taker, taker, initial)
