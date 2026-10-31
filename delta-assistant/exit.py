"""delta-exit: an exit plan for each open position (stop, break-even, trailing, target, warnings).

  python exit.py                                  # all open positions (API keys, read-only)
  python exit.py XAUTUSD sell --entry 4143.27 --size 5 [--stop 4200]   # offline

Rules used:
  * No stop on the exchange -> first priority: structure stop = beyond last 24 1h bars' extreme
    + 0.25 x ATR(1h).
  * Profit >= 1R -> move the stop to break-even (entry +/- fees).
  * Profit >= 2R -> trail: best price of last 22 1h bars -/+ 2 x ATR(1h).
  * 1h trend turned against the position -> flag it.
It prints `trade.py protect` / `close` commands as DRY RUNS. You decide and run them.
"""
import argparse

from delta_common import Delta, DeltaError, atr, fmt, load_rules, trend, uk_utc


def plan(sym, long, entry, size, stop, mark, cv, h1, funding):
    f = lambda x: fmt(x, entry)  # noqa: E731
    a = atr(h1)
    last24 = h1[-24:]
    fee = load_rules()["taker_fee_pct"] / 100
    structure = (min(c["low"] for c in last24) - 0.25 * a) if long else (max(c["high"] for c in last24) + 0.25 * a)
    sgn = 1 if long else -1
    at_stop = lambda px: (px - entry) * sgn * cv * size  # noqa: E731  P&L if stopped at px
    valid = lambda px: (px < mark) if long else (px > mark)  # noqa: E731  stop must sit beyond price
    better = lambda px: stop is None or (px - stop) * sgn > 0  # noqa: E731  only tighten, never loosen
    risk_px = abs(entry - stop) if stop else 1.5 * a
    pnl_px = (mark - entry) * sgn
    r_now = pnl_px / risk_px if risk_px else 0
    out = [f"{sym} {'LONG' if long else 'SHORT'} {size}  entry {f(entry)}  mark {f(mark)}  "
           f"P&L ${pnl_px * cv * size:.2f} ({r_now:+.2f}R{'' if stop else ', nominal 1R = 1.5 x 1h ATR'})  "
           f"1h ATR {f(a)}  1h trend {trend(h1)}"]
    cmds = []

    def suggest(label, px):
        if not valid(px):
            out.append(f"  {label} {f(px)}: price is already past it -> not usable")
        elif better(px):
            pnl = at_stop(px)
            out.append(f"  {label} {f(px)} -> if hit: {'+' if pnl >= 0 else '-'}${abs(pnl):.2f}"
                       f" ({'locks profit' if pnl > 0 else 'max loss'})")
            cmds.append(f"python trade.py protect {sym} --stop {f(px)}")

    if stop:
        out.append(f"  stop on exchange: {f(stop)} (if hit: ${at_stop(stop):+.2f})")
    else:
        out.append("  ⚠ NO STOP-LOSS on the exchange")
        suggest("structure stop (24h swing + 0.25 ATR)", structure)
    if r_now >= 1:
        suggest(">= 1R in profit: break-even stop", entry * (1 + 2 * fee) if long else entry * (1 - 2 * fee))
    if r_now >= 2:
        last22 = h1[-22:]
        suggest(">= 2R: trailing stop (22h extreme - 2 ATR)",
                (max(c["high"] for c in last22) - 2 * a) if long else (min(c["low"] for c in last22) + 2 * a))
    t = trend(h1)
    if (long and t == "DOWN") or (not long and t == "UP"):
        out.append(f"  ⚠ 1h trend is {t}: against your position")
    pays = (long and funding > 0) or (not long and funding < 0)
    out.append(f"  funding {funding:+.4f}%/8h -> you {'pay' if pays else 'receive'} about "
               f"${abs(funding) / 100 * mark * cv * size:.4f} per 8h")
    cmds.append(f"python trade.py close {sym}")
    return out, cmds


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbol", nargs="?")
    ap.add_argument("side", nargs="?", choices=["buy", "sell"])
    ap.add_argument("--entry", type=float)
    ap.add_argument("--size", type=int)
    ap.add_argument("--stop", type=float)
    a = ap.parse_args()
    pub = Delta()
    print(f"Exit plan {uk_utc()}")
    if a.symbol and a.side and a.entry and a.size:
        rows = [{"product_symbol": a.symbol.upper(), "size": a.size if a.side == "buy" else -a.size,
                 "entry_price": a.entry, "_stop": a.stop}]
    else:
        try:
            d = Delta(auth=True)
            rows = d.positions()
            for p in rows:
                sl = [o for o in d.open_orders(p["product_id"]) if o.get("stop_order_type") == "stop_loss_order"]
                p["_stop"] = float(sl[0]["stop_price"]) if sl else None
        except DeltaError as e:
            raise SystemExit(f"Give SYMBOL SIDE --entry --size (could not read positions: {e})")
    if not rows:
        print("No open positions.")
    for p in rows:
        sym = p["product_symbol"]
        tk, prod = pub.ticker(sym), pub.product(sym)
        lines, cmds = plan(sym, int(p["size"]) > 0, float(p["entry_price"]), abs(int(p["size"])), p.get("_stop"),
                           float(tk["mark_price"]), float(prod["contract_value"]), pub.candles(sym, "1h", 300),
                           float(tk.get("funding_rate") or 0))
        print("\n" + "\n".join(lines))
        print("  options (dry-run first; add --confirm only if YOU decide):")
        for c in cmds:
            print(f"    {c}")


if __name__ == "__main__":
    main()
