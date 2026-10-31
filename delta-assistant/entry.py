"""delta-entry: look for a rule-based entry on each symbol. Decides nothing for you — it shows
whether the written rules see a setup, with entry / stop / target / size / risk check.

  python entry.py                      # watchlist from rules.json
  python entry.py BTCUSD ETHUSD --balance 50

STRATEGY 1 — TREND PULLBACK (any time)
  * 4h and 1h trend agree (EMA20/50/200 stacked): both UP -> long only, both DOWN -> short only.
  * 15m price pulled back to within 0.5 x ATR(15m) of the 15m EMA20.
  * Last closed 15m candle closes back in the trend direction; RSI(15m) 40-65 long / 35-60 short.
  * Stop: beyond the last 12 15m bars' extreme + 0.25 ATR, at least 1 ATR from entry.
  * Target: 2R.

STRATEGY 2 — LONDON OPENING-RANGE BREAKOUT (09:00-12:00 UK, weekdays)
  * Range = high/low of the 08:00-09:00 UK 15m candles today; skip if wider than 1.5 x ATR(1h).
  * Last closed 15m candle closes outside the range with volume >= 1.2 x its 20-bar average.
  * Not against the 4h trend. Stop: middle of the range. Target: 2R.
"""
import argparse
from datetime import datetime, timedelta

from delta_common import UK, Delta, DeltaError, atr, ema_series, fmt, load_rules, now_utc, rsi, trend, uk_utc
from risk import account_state, check


def trend_pullback(sym, m15, h1, h4):
    t4, t1 = trend(h4), trend(h1)
    why = [f"4h {t4}, 1h {t1}"]
    if t4 != t1 or t4 == "MIXED":
        return None, why + ["trends don't agree -> no trade"]
    side = "buy" if t4 == "UP" else "sell"
    closes = [c["close"] for c in m15]
    e20 = ema_series(closes, 20)[-1]
    a = atr(m15)
    last = m15[-1]
    r = rsi(closes)
    near = abs(last["close"] - e20) <= 0.5 * a
    turn = last["close"] > last["open"] if side == "buy" else last["close"] < last["open"]
    rsi_ok = 40 <= r <= 65 if side == "buy" else 35 <= r <= 60
    why.append(f"15m close {fmt(last['close'])} vs EMA20 {fmt(e20, last['close'])} "
               f"({'near' if near else 'not near'}, ATR {fmt(a, last['close'])}), RSI {r:.0f}, "
               f"last candle {'turned' if turn else 'did not turn'} {'up' if side == 'buy' else 'down'}")
    if not (near and turn and rsi_ok):
        return None, why + ["no pullback entry yet"]
    entry = last["close"]
    recent = m15[-12:]
    if side == "buy":
        stop = min(min(c["low"] for c in recent) - 0.25 * a, entry - a)
    else:
        stop = max(max(c["high"] for c in recent) + 0.25 * a, entry + a)
    target = entry + 2 * (entry - stop)
    return {"strategy": "trend-pullback", "side": side, "entry": entry, "stop": stop, "target": target}, why


def london_breakout(sym, m15, h1, h4):
    now = now_utc().astimezone(UK)
    if now.strftime("%a") in ("Sat", "Sun") or not ("09:00" <= now.strftime("%H:%M") < "12:00"):
        return None, ["London breakout only runs 09:00-12:00 UK on weekdays"]
    day = now.replace(hour=8, minute=0, second=0, microsecond=0)
    rng = [c for c in m15 if day <= datetime.fromtimestamp(c["time"], UK) < day + timedelta(hours=1)]
    if len(rng) < 4:
        return None, ["08:00-09:00 UK range not complete"]
    hi, lo = max(c["high"] for c in rng), min(c["low"] for c in rng)
    a1 = atr(h1)
    why = [f"range {fmt(lo)}-{fmt(hi, lo)} (width {fmt(hi - lo, lo)}, 1h ATR {fmt(a1, lo)})"]
    if hi - lo > 1.5 * a1:
        return None, why + ["range too wide -> skip"]
    last = m15[-1]
    vols = [c["volume"] for c in m15]
    vol_ok = vols[-1] >= 1.2 * (sum(vols[-21:-1]) / 20 or 1)
    t4 = trend(h4)
    side = "buy" if last["close"] > hi else "sell" if last["close"] < lo else None
    if not side:
        return None, why + ["price still inside the range"]
    if (side == "buy" and t4 == "DOWN") or (side == "sell" and t4 == "UP"):
        return None, why + [f"breakout {side} is against the 4h {t4} trend"]
    if not vol_ok:
        return None, why + ["breakout without volume"]
    entry, stop = last["close"], (hi + lo) / 2
    return {"strategy": "london-breakout", "side": side, "entry": entry, "stop": stop,
            "target": entry + 2 * (entry - stop)}, why


def main():
    rules = load_rules()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="*", default=rules["watchlist"])
    ap.add_argument("--balance", type=float, help="skip the API read; also sets available")
    a = ap.parse_args()
    d = Delta()
    if a.balance is not None:
        acct = {"balance": a.balance, "available": a.balance, "open": 0, "symbols": [], "day_pnl": 0.0}
    else:
        try:
            acct = account_state(Delta(auth=True))
        except DeltaError as e:
            raise SystemExit(f"Pass --balance (could not read account: {e})")
    print(f"Entry check {uk_utc()}  | balance ${acct['balance']:.2f}  open {acct['open']}  "
          f"today ${acct['day_pnl']:.2f}")
    for sym in (s.upper() for s in a.symbols):
        try:
            m15, h1, h4 = d.candles(sym, "15m", 300), d.candles(sym, "1h", 300), d.candles(sym, "4h", 300)
        except DeltaError as e:
            print(f"\n=== {sym}  ERROR {e}")
            continue
        print(f"\n=== {sym}")
        found = []
        for fn in (trend_pullback, london_breakout):
            setup, why = fn(sym, m15, h1, h4)
            print(f"  {fn.__name__.replace('_', ' ')}: " + "; ".join(why))
            if setup:
                found.append(setup)
        for s in found:
            verdict, lines, p = check(sym, s["side"], s["entry"], s["stop"], s["target"], None, None, acct, rules)
            f = lambda x: fmt(x, s["entry"])  # noqa: E731
            print(f"  >> SETUP {s['strategy']}: {s['side'].upper()} entry {f(s['entry'])} stop {f(s['stop'])} "
                  f"target {f(s['target'])} | size {p['contracts']} | risk check {verdict}")
            for st, t in lines:
                if st != "PASS":
                    print(f"     [{st}] {t}")
            if verdict == "PASS":
                print(f"     ticket: python trade.py {s['side']} {sym} --contracts {p['contracts']} "
                      f"--stop {f(s['stop'])} --target {f(s['target'])} --strategy {s['strategy']}")
        if not found:
            print("  -> no setup")


if __name__ == "__main__":
    main()
