"""delta-backtest: replay the trend-pullback rule (strategy.py, same code as live) over past data.

  python backtest.py                         # BTC ETH SOL XRP, last 60 days
  python backtest.py ETHUSD --days 90 --json results.json

How it simulates (honest, slightly pessimistic):
  * Signal on a closed 15m candle; entry at the NEXT candle's open (you can't fill on the close).
  * Stop and 2R target fixed at entry. If both are touched in one candle -> counted as a STOP.
  * Exit at the close after 24h if neither was hit. One trade per symbol at a time.
  * Taker fees both ways are subtracted (in R). No slippage beyond that, no funding.
Results: trades, win rate, average R and total R — for every base signal, and split by grade A/B/C.
Past results don't guarantee future results.
"""
import argparse
import bisect
import json

from delta_common import Delta, load_rules, swings
from strategy import base_signal, prepare, quality, trend_list

HORIZON = 96  # 15m candles = 24h


def at_or_before(times, t):
    j = bisect.bisect_right(times, t) - 1
    return j if j >= 0 else None


def run(sym, days, fee):
    d = Delta()
    m15 = d.candles(sym, "15m", days * 96 + 300)
    h1 = d.candles(sym, "1h", days * 24 + 260)
    h4 = d.candles(sym, "4h", days * 6 + 230)
    fund = d.candles(f"FUNDING:{sym}", "1h", days * 24 + 10)
    oi = d.candles(f"OI:{sym}", "1h", days * 24 + 10)
    ind = prepare(m15)
    t1s, t4s = trend_list(h1), trend_list(h4)
    # a 1h/4h bar is usable once it has CLOSED: its open time + length <= 15m bar close time
    h1_close = [r["time"] + 3600 for r in h1]
    h4_close = [r["time"] + 14400 for r in h4]
    f_t, o_t = [r["time"] + 3600 for r in fund], [r["time"] + 3600 for r in oi]
    trades, busy_until = [], -1
    for i in range(250, len(m15) - 1):
        if i <= busy_until:
            continue
        now = m15[i]["time"] + 900
        j1, j4 = at_or_before(h1_close, now), at_or_before(h4_close, now)
        if j1 is None or j4 is None:
            continue
        setup, _ = base_signal(m15, ind, i, t4s[j4], t1s[j1])
        if not setup:
            continue
        jf, jo = at_or_before(f_t, now), at_or_before(o_t, now)
        funding = fund[jf]["close"] if jf is not None else None
        oi_chg = ((oi[jo]["close"] / oi[jo - 4]["close"] - 1) * 100
                  if jo is not None and jo >= 4 and oi[jo - 4]["close"] else None)
        grade, checks = quality(setup, m15, ind, swings(h1[:j1 + 1]), funding, oi_chg)
        # simulate from the next candle's open
        side, stop, target = setup["side"], setup["stop"], setup["target"]
        entry = m15[i + 1]["open"]
        risk = abs(entry - stop)
        if risk <= 0 or (side == "sell" and entry >= stop) or (side == "buy" and entry <= stop):
            continue
        result, exit_px, k = "time", None, i + 1
        for k in range(i + 1, min(i + 1 + HORIZON, len(m15))):
            b = m15[k]
            hit_stop = b["high"] >= stop if side == "sell" else b["low"] <= stop
            hit_tgt = b["low"] <= target if side == "sell" else b["high"] >= target
            if hit_stop:
                result, exit_px = "stop", stop
                break
            if hit_tgt:
                result, exit_px = "target", target
                break
        if exit_px is None:
            exit_px = m15[k]["close"]
        gross = (entry - exit_px) if side == "sell" else (exit_px - entry)
        r = gross / risk - (entry + exit_px) * fee / risk
        trades.append({"sym": sym, "time": m15[i]["time"], "side": side, "grade": grade,
                       "checks": [ok for _, ok, _ in checks], "result": result, "r": round(r, 3)})
        busy_until = k
    return trades


def stats(rows):
    n = len(rows)
    if not n:
        return {"trades": 0, "win": 0.0, "avg_r": 0.0, "total_r": 0.0}
    wins = sum(1 for t in rows if t["r"] > 0)
    tot = sum(t["r"] for t in rows)
    return {"trades": n, "win": wins / n * 100, "avg_r": tot / n, "total_r": tot}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="*", default=["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD"])
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--json", help="also write all trades + summary here")
    a = ap.parse_args()
    fee = load_rules()["taker_fee_pct"] / 100
    trades = []
    for s in (x.upper() for x in a.symbols):
        t = run(s, a.days, fee)
        trades += t
        print(f"{s}: {len(t)} base signals")
    groups = {
        "Base rule (all signals)": trades,
        "Grade A only": [t for t in trades if t["grade"] == "A"],
        "Grade A + B": [t for t in trades if t["grade"] in "AB"],
        "Grade C": [t for t in trades if t["grade"] == "C"],
    }
    names = ["rejection candle", "volume on the turn", "room to target", "futures agree"]
    for n_i, name in enumerate(names):
        groups[f"'{name}' passes"] = [t for t in trades if t["checks"][n_i]]
    print(f"\nTrend pullback, last {a.days} days, {', '.join(a.symbols)} (fees included, entry next open)")
    print(f"{'group':38} {'trades':>6} {'win %':>6} {'avg R':>7} {'total R':>8}")
    summary = {}
    for g, rows in groups.items():
        s = stats(rows)
        summary[g] = s
        print(f"{g:38} {s['trades']:6d} {s['win']:6.1f} {s['avg_r']:+7.2f} {s['total_r']:+8.1f}")
    print("\nSimulation on past data. Small samples are noisy; not a promise of future results.")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump({"summary": summary, "trades": trades}, fh)


if __name__ == "__main__":
    main()
