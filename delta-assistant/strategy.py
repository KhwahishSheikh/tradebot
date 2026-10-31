"""The trend-pullback rule and its quality checks, in ONE place.
entry.py (live) and backtest.py (history) both call these functions, so what you test is what you trade.

BASE RULE (must all be true)
  * 4h and 1h trend agree (EMA20/50/200 stacked) -> trade only in that direction.
  * Last closed 15m candle closes within 0.5 x ATR(15m) of the 15m EMA20 (the pullback zone).
  * That candle closes in the trend direction; RSI(15m) 40-65 for longs / 35-60 for shorts.
  * Stop: beyond the last 12 candles' extreme + 0.25 ATR, at least 1 ATR from entry. Target: 2R.

QUALITY CHECKS — grade set from the 60-day backtest (31 Oct 2026, BTC/ETH/SOL/XRP):
  A = volume + futures + room to target   (6 trades:  +0.77R avg — tiny sample)
  B = volume + futures                    (76 trades: +0.09R avg, but -0.13R in the first 30 days)
  C = anything else                       (base rule overall: 302 trades, -0.11R avg)
  Check 1 (rejection candle) is shown for information only: it made results WORSE in the test.
  UNPROVEN: re-run backtest.py regularly; trade it on testnet before real money.
  1. Rejection candle: wick against the trade >= 40% of the candle, or it engulfs the previous candle.
  2. Volume on the turn: candle volume >= its 20-candle average.
  3. Room to target: no 1h swing level between entry and the 2R target.
  4. Futures agree: funding not crowded on our side (short: >= -0.01%, long: <= +0.03% per 8h)
     and open interest not dropping fast (4h change > -3%).
"""


def ema_list(values, n):
    k, out, e = 2 / (n + 1), [], None
    for i, v in enumerate(values):
        if i == n - 1:
            e = sum(values[:n]) / n
        elif i >= n:
            e = v * k + e * (1 - k)
        out.append(e)
    return out


def atr_list(rows, n=14):
    out, a = [None], None
    for i in range(1, len(rows)):
        c, p = rows[i], rows[i - 1]
        tr = max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
        if i < n:
            out.append(None)
            a = (a or 0) + tr
            continue
        a = (a + tr) / n if i == n else (a * (n - 1) + tr) / n
        out.append(a)
    return out


def rsi_list(closes, n=14):
    out, g, l = [None] * len(closes), 0.0, 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        up, dn = max(d, 0), max(-d, 0)
        if i <= n:
            g, l = g + up, l + dn
            if i == n:
                g, l = g / n, l / n
                out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
            continue
        g, l = (g * (n - 1) + up) / n, (l * (n - 1) + dn) / n
        out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def trend_list(rows):
    """UP / DOWN / MIXED for every bar (EMA20/50/200 stacked with price)."""
    c = [r["close"] for r in rows]
    e20, e50, e200 = ema_list(c, 20), ema_list(c, 50), ema_list(c, 200)
    out = []
    for i, x in enumerate(c):
        if e200[i] is None:
            out.append(None)
        elif x > e50[i] > e200[i] and e20[i] > e50[i]:
            out.append("UP")
        elif x < e50[i] < e200[i] and e20[i] < e50[i]:
            out.append("DOWN")
        else:
            out.append("MIXED")
    return out


def prepare(m15):
    c = [r["close"] for r in m15]
    return {"c": c, "e20": ema_list(c, 20), "atr": atr_list(m15), "rsi": rsi_list(c),
            "vol": [r["volume"] for r in m15]}


def base_signal(m15, ind, i, t4, t1):
    """Base rule on closed 15m bar i given the 4h/1h trends at that time. -> (setup|None, reasons)."""
    why = [f"4h {t4}, 1h {t1}"]
    if t4 != t1 or t4 in (None, "MIXED"):
        return None, why + ["trends don't agree"]
    side = "buy" if t4 == "UP" else "sell"
    bar, e20, a, r = m15[i], ind["e20"][i], ind["atr"][i], ind["rsi"][i]
    if e20 is None or a is None or r is None:
        return None, why + ["not enough history"]
    near = abs(bar["close"] - e20) <= 0.5 * a
    turn = bar["close"] > bar["open"] if side == "buy" else bar["close"] < bar["open"]
    rsi_ok = 40 <= r <= 65 if side == "buy" else 35 <= r <= 60
    why.append(f"15m close {bar['close']:g} vs EMA20 {e20:.2f} ({'in zone' if near else 'outside zone'}, "
               f"ATR {a:.2f}), RSI {r:.0f}, candle {'turned' if turn else 'did not turn'}")
    if not (near and turn and rsi_ok):
        return None, why + ["no pullback entry"]
    entry = bar["close"]
    recent = m15[max(0, i - 11):i + 1]
    if side == "buy":
        stop = min(min(x["low"] for x in recent) - 0.25 * a, entry - a)
    else:
        stop = max(max(x["high"] for x in recent) + 0.25 * a, entry + a)
    return {"strategy": "trend-pullback", "side": side, "entry": entry, "stop": stop,
            "target": entry + 2 * (entry - stop), "i": i}, why


def quality(setup, m15, ind, h1_levels, funding, oi_chg_4h):
    """The 4 checks -> (grade, [(name, ok, detail)]). h1_levels = (swing highs, swing lows) of closed 1h bars."""
    i, side = setup["i"], setup["side"]
    bar, prev = m15[i], m15[i - 1]
    rng = (bar["high"] - bar["low"]) or 1e-12
    if side == "sell":
        wick = (bar["high"] - max(bar["open"], bar["close"])) / rng
        engulf = prev["close"] > prev["open"] and bar["close"] <= prev["open"] and bar["open"] >= prev["close"]
    else:
        wick = (min(bar["open"], bar["close"]) - bar["low"]) / rng
        engulf = prev["close"] < prev["open"] and bar["close"] >= prev["open"] and bar["open"] <= prev["close"]
    vols = ind["vol"][max(0, i - 20):i]
    vavg = sum(vols) / len(vols) if vols else 0
    lo, hi = sorted((setup["entry"], setup["target"]))
    highs, lows = h1_levels
    blockers = [x for x in (lows if side == "sell" else highs) if lo < x < hi]
    crowded = (funding is not None) and ((side == "sell" and funding < -0.01) or (side == "buy" and funding > 0.03))
    oi_drop = (oi_chg_4h is not None) and oi_chg_4h <= -3
    checks = [
        ("rejection candle", wick >= 0.4 or engulf, f"wick {wick * 100:.0f}% of candle" + (", engulfing" if engulf else "")),
        ("volume on the turn", vavg > 0 and bar["volume"] >= vavg, f"volume x{bar['volume'] / vavg:.2f} of 20-candle avg" if vavg else "no volume data"),
        ("room to target", not blockers, "clear path" if not blockers else f"1h level in the way at {blockers[0]:g}"),
        ("futures agree", not crowded and not oi_drop,
         f"funding {funding if funding is not None else 0:+.4f}%, OI 4h {oi_chg_4h if oi_chg_4h is not None else 0:+.1f}%"),
    ]
    vol_ok, room_ok, fut_ok = checks[1][1], checks[2][1], checks[3][1]
    grade = "A" if vol_ok and fut_ok and room_ok else "B" if vol_ok and fut_ok else "C"
    return grade, checks
