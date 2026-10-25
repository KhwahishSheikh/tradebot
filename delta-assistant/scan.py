"""Market scan numbers for the "scan" command. Public market data only (no API keys, no orders).

Usage:
  python scan.py --balance 35.29                    # default symbols
  python scan.py --balance 35.29 BTCUSD ETHUSD      # pick symbols

Prints, per symbol: price, funding, OI, and for 4h/1h/15m the EMA 20/50/200 trend, RSI14, ATR14,
volume vs 20-bar average, and nearest swing support/resistance. Plus what 1 contract costs at
MAX_LEVERAGE and the widest stop that keeps risk within RISK_PCT of --balance.
Claude reads this and applies the strategy rules in CLAUDE.md — the script decides nothing.
"""
import argparse
import time
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://api.india.delta.exchange"
DEFAULT_SYMBOLS = ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "DOGEUSD"]
TIMEFRAMES = {"4h": 14400, "1h": 3600, "15m": 900}
BARS = 400  # enough history for EMA200
MAX_LEVERAGE = 3
RISK_PCT = 1.0
TAKER_FEE = 0.0005  # per side, from Delta product specs


def get(path, **params):
    r = requests.get(BASE + path, params=params, timeout=10)
    r.raise_for_status()
    return r.json()["result"]


def candles(symbol, res):
    end = int(time.time())
    rows = get("/v2/history/candles", resolution=res, symbol=symbol, start=end - TIMEFRAMES[res] * BARS, end=end)
    return sorted(rows, key=lambda c: c["time"])[:-1]  # drop the still-forming bar


def ema(values, n):
    k, e = 2 / (n + 1), sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


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
    """Fractal swing highs/lows: a bar higher/lower than the k bars on each side."""
    rows = rows[-lookback:]
    highs = [rows[i]["high"] for i in range(k, len(rows) - k)
             if rows[i]["high"] == max(r["high"] for r in rows[i - k:i + k + 1])]
    lows = [rows[i]["low"] for i in range(k, len(rows) - k)
            if rows[i]["low"] == min(r["low"] for r in rows[i - k:i + k + 1])]
    return highs, lows


def trend(close, e20, e50, e200):
    if close > e50 > e200 and e20 > e50:
        return "UP"
    if close < e50 < e200 and e20 < e50:
        return "DOWN"
    return "MIXED"


def next_funding(now):
    """Delta funding every 8h at 00:00 / 08:00 / 16:00 UTC."""
    nxt = now.replace(minute=0, second=0, microsecond=0, hour=(now.hour // 8) * 8) + timedelta(hours=8)
    return nxt, int((nxt - now).total_seconds() // 60)


def fmt(x, ref):
    digits = 1 if ref >= 1000 else 2 if ref >= 10 else 4 if ref >= 0.1 else 6
    return f"{x:.{digits}f}"


def scan_symbol(sym, balance):
    tk, prod = get(f"/v2/tickers/{sym}"), get(f"/v2/products/{sym}")
    mark, cv = float(tk["mark_price"]), float(prod["contract_value"])
    f = lambda x: fmt(x, mark)  # noqa: E731
    fund = float(tk.get("funding_rate") or 0)
    print(f"\n=== {sym}  mark {f(mark)}  24h {float(tk.get('mark_change_24h') or 0):+.2f}%  "
          f"funding {fund:+.4f}%/8h ({'longs pay' if fund > 0 else 'shorts pay' if fund < 0 else 'flat'})  "
          f"OI ${float(tk.get('oi_value_usd') or 0) / 1e6:.1f}M  24h vol ${float(tk.get('turnover_usd') or 0) / 1e6:.0f}M")
    atr_1h = None
    for res in TIMEFRAMES:
        rows = candles(sym, res)
        closes, vols = [c["close"] for c in rows], [c["volume"] for c in rows]
        e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
        a = atr(rows)
        if res == "1h":
            atr_1h = a
        highs, lows = swings(rows)
        res_lvl = min((h for h in highs if h > mark), default=None)
        sup_lvl = max((lo for lo in lows if lo < mark), default=None)
        avg_vol = sum(vols[-21:-1]) / 20 or 1
        print(f"  {res:>3}  {trend(closes[-1], e20, e50, e200):5}  close {f(closes[-1])}  "
              f"ema20 {f(e20)} ema50 {f(e50)} ema200 {f(e200)}  rsi {rsi(closes):4.1f}  "
              f"atr {f(a)} ({a / mark * 100:.2f}%)  vol x{vols[-1] / avg_vol:.2f}  "
              f"sup {f(sup_lvl) if sup_lvl else '-'}  res {f(res_lvl) if res_lvl else '-'}")
    notional = mark * cv
    margin = notional / MAX_LEVERAGE
    line = f"  1 contract = {cv:g} {prod['underlying_asset']['symbol']} ≈ ${notional:.2f}, margin @{MAX_LEVERAGE}x ${margin:.2f}"
    if balance:
        risk_usd = balance * RISK_PCT / 100
        fees = notional * TAKER_FEE * 2
        max_stop = (risk_usd - fees) / notional * 100
        line += (f"  | {RISK_PCT:g}% risk = ${risk_usd:.2f} → widest stop for 1 contract {max_stop:.2f}%"
                 f" (1h ATR = {atr_1h / mark * 100:.2f}%)")
        if margin > balance * 0.5:
            line += "  ⚠ margin > 50% of balance"
    print(line)


def main():
    ap = argparse.ArgumentParser(description="Numbers for the scan command (public data only)")
    ap.add_argument("symbols", nargs="*", default=DEFAULT_SYMBOLS)
    ap.add_argument("--balance", type=float, help="wallet balance in USD (read it via MCP)")
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    nxt, mins = next_funding(now)
    print(f"Scan {now:%Y-%m-%d %H:%M} UTC  | next funding {nxt:%H:%M} UTC (in {mins} min)"
          + (f"  | balance ${a.balance:.2f}" if a.balance else ""))
    for sym in a.symbols:
        try:
            scan_symbol(sym.upper(), a.balance)
        except (requests.RequestException, KeyError, ValueError, ZeroDivisionError) as e:
            print(f"\n=== {sym}  ERROR {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
