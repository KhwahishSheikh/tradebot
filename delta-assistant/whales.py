"""delta-whales: what are the big players doing? (the crypto version of tracking insiders' trades).
Public Delta data only, no keys.

  python whales.py                     # watchlist from rules.json
  python whales.py BTCUSD --hours 24

Per symbol:
  * BIG TRADES NOW  the latest trades Delta publishes (only ~50, so seconds to minutes):
    trades >= --min-usd, which side was the aggressor (taker), net buy/sell flow.
  * VOLUME BURSTS  5-minute candles in the last --hours with volume >= 4x the median:
    big money hitting the market at once, and which way price went.
  * OI JUMPS  1-hour bars where open interest changed >= 3% (or >= 2.5 std devs):
    big positions opening or closing, labelled with the price move.
  * FUNDING SHIFTS  hours where funding moved by >= 0.005%/8h: one side crowding in.
Descriptive only. Big players can be wrong, hedging, or market makers.
"""
import argparse
import statistics
from datetime import datetime

from delta_common import UK, Delta, DeltaError, fmt, load_rules, uk_utc

from analyst import oi_read, pct


def when(ts_seconds):
    return datetime.fromtimestamp(ts_seconds, UK).strftime("%a %H:%M")


def big_trades(d, sym, cv, min_usd):
    rows = d.call("GET", f"/v2/trades/{sym}", auth=False)
    if not rows:
        return
    buy = sell = 0.0
    big = []
    for t in rows:
        usd = float(t["price"]) * t["size"] * cv
        taker_buy = t.get("buyer_role") == "taker"
        buy, sell = (buy + usd, sell) if taker_buy else (buy, sell + usd)
        if usd >= min_usd:
            big.append((t["timestamp"] // 1_000_000, "BUY " if taker_buy else "SELL", usd, float(t["price"])))
    span = (rows[0]["timestamp"] - rows[-1]["timestamp"]) / 1e6
    print(f"  BIG TRADES NOW  last {len(rows)} trades over {span:.0f}s: aggressive buys ${buy / 1e3:,.0f}k vs "
          f"sells ${sell / 1e3:,.0f}k ({'buyers' if buy > sell else 'sellers'} pushing)")
    for ts, side, usd, px in big[:8]:
        print(f"      {when(ts)} UK  {side} ${usd / 1e3:,.0f}k @ {fmt(px)}")
    if not big:
        print(f"      none >= ${min_usd / 1e3:,.0f}k right now")


def bursts(d, sym, hours):
    rows = d.candles(sym, "5m", int(hours * 12))
    vols = [c["volume"] for c in rows]
    med = statistics.median(vols) if vols else 0
    hits = [c for c in rows if med and c["volume"] >= 4 * med]
    print(f"  VOLUME BURSTS   {len(hits)} five-minute candles >= 4x normal volume in {hours:g}h")
    for c in hits[-8:]:
        move = pct(c["close"], c["open"])
        print(f"      {when(c['time'])} UK  vol x{c['volume'] / med:.0f}  price {move:+.2f}%  "
              f"({'buying burst' if move > 0.02 else 'selling burst' if move < -0.02 else 'two-way, price flat'})")


def oi_jumps(d, sym, hours):
    oi = d.candles(f"OI:{sym}", "1h", int(hours) + 1)
    px = {c["time"]: c["close"] for c in d.candles(f"MARK:{sym}", "1h", int(hours) + 2)}
    if len(oi) < 3:
        print("  OI JUMPS        no data")
        return
    ch = [(b["time"], pct(b["close"], a["close"]), a["time"]) for a, b in zip(oi, oi[1:])]
    sd = statistics.pstdev([c for _, c, _ in ch]) or 1
    hits = [x for x in ch if abs(x[1]) >= 3 or abs(x[1]) >= 2.5 * sd]
    print(f"  OI JUMPS        {len(hits)} hours with a big open-interest change")
    for t, c, prev in hits[-8:]:
        dp = pct(px.get(t, 0), px.get(prev, 0)) if px.get(t) and px.get(prev) else 0
        print(f"      {when(t)} UK  OI {c:+.1f}%  price {dp:+.2f}%  -> {oi_read(dp, c)}")


def funding_shifts(d, sym, hours):
    fr = d.candles(f"FUNDING:{sym}", "1h", int(hours) + 1)
    hits = [(b["time"], a["close"], b["close"]) for a, b in zip(fr, fr[1:]) if abs(b["close"] - a["close"]) >= 0.005]
    print(f"  FUNDING SHIFTS  {len(hits)} in {hours:g}h")
    for t, a, b in hits[-6:]:
        print(f"      {when(t)} UK  {a:+.4f}% -> {b:+.4f}%  ({'longs crowding in' if b > a else 'shorts crowding in'})")


def main():
    rules = load_rules()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="*", default=rules["watchlist"])
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--min-usd", type=float, default=50_000, help="big-trade threshold in $ (default 50k)")
    a = ap.parse_args()
    print(f"Whale watch {uk_utc()}  last {a.hours:g}h  (descriptive only, not a signal)")
    d = Delta()
    for sym in (s.upper() for s in a.symbols):
        print(f"\n=== {sym}")
        try:
            cv = float(d.product(sym)["contract_value"])
            big_trades(d, sym, cv, a.min_usd)
            bursts(d, sym, a.hours)
            oi_jumps(d, sym, a.hours)
            funding_shifts(d, sym, a.hours * 7 if a.hours < 72 else a.hours)
        except (DeltaError, KeyError, ValueError) as e:
            print(f"  ERROR {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
