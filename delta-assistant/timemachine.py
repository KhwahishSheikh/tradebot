"""delta-timemachine: how often would this trade have hit target vs stop? Two views:

  python timemachine.py BTCUSD buy --stop-pct 0.6 --target-pct 1.2 [--hours 24] [--days 60] [--sims 10000]
  python timemachine.py BTCUSD buy --entry 80100 --stop 79620 --target 81060

1. REPLAY: enter at the open of every past 15m candle (last --days) with the same % stop/target,
   walk forward up to --hours. Real history, overlapping samples.
2. MONTE CARLO: build --sims random future paths by stitching random 8-candle blocks of real
   15m history (keeps volatility clustering), same stop/target/horizon.
If stop and target are touched in the same candle it counts as STOP (pessimistic).
These are simulations on past data, NOT a track record or a prediction.
"""
import argparse
import random

from delta_common import Delta, load_rules

BLOCK = 8


def outcome(path, side, stop_pct, tgt_pct):
    """path: list of (high, low) as fractions vs entry. Returns 'target'|'stop'|'open', bars."""
    for i, (h, lo) in enumerate(path):
        hit_stop = lo <= -stop_pct if side == "buy" else h >= stop_pct
        hit_tgt = h >= tgt_pct if side == "buy" else lo <= -tgt_pct
        if hit_stop:
            return "stop", i + 1
        if hit_tgt:
            return "target", i + 1
    return "open", len(path)


def summarize(name, results, stop_pct, tgt_pct, fee_pct, last_close):
    n = len(results)
    t = sum(1 for r, _ in results if r == "target")
    s = sum(1 for r, _ in results if r == "stop")
    o = n - t - s
    r_mult = tgt_pct / stop_pct
    fee_r = 2 * fee_pct / stop_pct
    open_r = sum(c for r, c in last_close if r == "open") / max(o, 1) / stop_pct if o else 0
    exp = (t * r_mult - s + o * open_r) / n - fee_r
    print(f"\n{name}  ({n:,} samples)")
    print(f"  target first : {t / n * 100:5.1f}%")
    print(f"  stop first   : {s / n * 100:5.1f}%")
    print(f"  still open   : {o / n * 100:5.1f}%  (avg {open_r:+.2f}R at the horizon)")
    print(f"  avg result   : {exp:+.2f}R per trade after fees  (target = +{r_mult:.2f}R, fees {fee_r:.2f}R)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbol")
    ap.add_argument("side", choices=["buy", "sell"])
    ap.add_argument("--entry", type=float)
    ap.add_argument("--stop", type=float)
    ap.add_argument("--target", type=float)
    ap.add_argument("--stop-pct", type=float)
    ap.add_argument("--target-pct", type=float)
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--sims", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    sym = a.symbol.upper()
    if a.stop_pct and a.target_pct:
        stop_pct, tgt_pct = a.stop_pct / 100, a.target_pct / 100
    elif a.entry and a.stop and a.target:
        stop_pct, tgt_pct = abs(a.entry - a.stop) / a.entry, abs(a.target - a.entry) / a.entry
    else:
        raise SystemExit("Give --stop-pct and --target-pct, or --entry --stop --target")
    fee_pct = load_rules()["taker_fee_pct"] / 100  # per side, as a fraction of price
    bars = Delta().candles(sym, "15m", a.days * 96)
    horizon = int(a.hours * 4)
    print(f"{sym} {a.side.upper()}  stop {stop_pct * 100:.2f}%  target {tgt_pct * 100:.2f}%  "
          f"horizon {a.hours:g}h  history {len(bars)} x 15m candles")

    # per-candle moves relative to the candle's open: (high, low, close) fractions
    rel = [((c["high"] - c["open"]) / c["open"], (c["low"] - c["open"]) / c["open"],
            (c["close"] - c["open"]) / c["open"]) for c in bars]

    # 1) replay
    res, closes = [], []
    for i in range(len(bars) - horizon):
        e = bars[i]["open"]
        path = [((bars[j]["high"] - e) / e, (bars[j]["low"] - e) / e) for j in range(i, i + horizon)]
        r = outcome(path, a.side, stop_pct, tgt_pct)
        res.append(r)
        if r[0] == "open":
            ch = (bars[i + horizon - 1]["close"] - e) / e
            closes.append(("open", ch if a.side == "buy" else -ch))
    summarize("REPLAY of real history", res, stop_pct, tgt_pct, fee_pct, closes)

    # 2) monte carlo with block bootstrap
    rnd = random.Random(a.seed)
    res, closes = [], []
    for _ in range(a.sims):
        moves = []
        while len(moves) < horizon:
            k = rnd.randrange(0, len(rel) - BLOCK)
            moves.extend(rel[k:k + BLOCK])
        level, path = 1.0, []
        for h, lo, c in moves[:horizon]:
            path.append((level * (1 + h) - 1, level * (1 + lo) - 1))
            level *= 1 + c
        r = outcome(path, a.side, stop_pct, tgt_pct)
        res.append(r)
        if r[0] == "open":
            ch = level - 1
            closes.append(("open", ch if a.side == "buy" else -ch))
    summarize(f"MONTE CARLO ({a.sims:,} random paths)", res, stop_pct, tgt_pct, fee_pct, closes)
    print("\nSimulation on past data only. Not a track record, not a prediction.")


if __name__ == "__main__":
    main()
