"""delta-analyst: health check of a coin's futures market (the crypto version of reading a company's
numbers). Public data only, no keys.

  python analyst.py                    # watchlist from rules.json
  python analyst.py BTCUSD ETHUSD

Per symbol:
  * FUNDING  now, 7-day average, rank vs last 30 days, who pays, yearly cost.
  * OPEN INTEREST  change over 4h / 24h / 7d, read together with price:
      price up + OI up = new longs | price up + OI down = shorts closing
      price down + OI up = new shorts | price down + OI down = longs closing
  * BASIS  perpetual (mark) vs spot index: positive = futures priced above spot (bullish crowd).
  * ORDER BOOK  spread, $ depth within 0.5% / 1% each side, buy/sell imbalance, slippage of a
    $10k market order.
  * ACTIVITY  24h volume vs open interest.
Descriptive only: it shows positioning, it does not say what to trade.
"""
import argparse

from delta_common import Delta, DeltaError, fmt, load_rules, uk_utc


def pct(a, b):
    return (a - b) / b * 100 if b else 0.0


def at_hours_ago(rows, hours):
    """Close of the bar `hours` before the last one (rows are 1h bars)."""
    return rows[-1 - hours]["close"] if len(rows) > hours else rows[0]["close"]


def oi_read(dp, doi):
    if abs(doi) < 1:
        return "little change in positioning"
    if dp >= 0 and doi > 0:
        return "new LONGS opening (money coming in behind the rise)"
    if dp >= 0 and doi < 0:
        return "SHORTS closing (rise driven by short covering)"
    if dp < 0 and doi > 0:
        return "new SHORTS opening (money coming in behind the fall)"
    return "LONGS closing / being liquidated"


def depth(book, mid, band):
    lo, hi = mid * (1 - band), mid * (1 + band)
    bid = sum(float(b["price"]) * b["size"] for b in book["buy"] if float(b["price"]) >= lo)
    ask = sum(float(a["price"]) * a["size"] for a in book["sell"] if float(a["price"]) <= hi)
    return bid, ask


def slippage(levels, cv, usd):
    """Avg fill vs best price for a market order of `usd` notional walking `levels`."""
    left, cost, qty = usd, 0.0, 0.0
    for lv in levels:
        px = float(lv["price"])
        take = min(left, px * lv["size"] * cv)
        cost += take
        qty += take / px
        left -= take
        if left <= 0:
            break
    if left > 0 or not qty:
        return None
    return abs(cost / qty - float(levels[0]["price"])) / float(levels[0]["price"]) * 100


def analyse(d, sym):
    tk, prod = d.ticker(sym), d.product(sym)
    cv = float(prod["contract_value"])
    mark = float(tk["mark_price"])
    spot = float(tk.get("spot_price") or mark)
    f = lambda x: fmt(x, mark)  # noqa: E731
    print(f"\n=== {sym}  mark {f(mark)}  24h {float(tk.get('mark_change_24h') or 0):+.2f}%")

    # funding (% per 8h)
    fund_now = float(tk.get("funding_rate") or 0)
    fr = [c["close"] for c in d.candles(f"FUNDING:{sym}", "1h", 24 * 30)]
    if fr:
        avg7 = sum(fr[-24 * 7:]) / len(fr[-24 * 7:])
        rank = sum(1 for x in fr if x < fund_now) / len(fr) * 100
        who = "longs pay shorts" if fund_now > 0 else "shorts pay longs" if fund_now < 0 else "nobody pays"
        flag = ("  ⚠ CROWDED LONG" if fund_now >= 0.03 else "  ⚠ CROWDED SHORT" if fund_now <= -0.01 else "")
        print(f"  FUNDING   now {fund_now:+.4f}%/8h ({who}, ~{fund_now * 3 * 365:+.1f}%/yr)  7d avg {avg7:+.4f}%  "
              f"higher than {rank:.0f}% of last 30d{flag}")

    # open interest vs price
    oi = d.candles(f"OI:{sym}", "1h", 24 * 8)
    px = d.candles(f"MARK:{sym}", "1h", 24 * 8)
    if oi and px:
        oi_now = oi[-1]["close"]
        parts = []
        for h, label in ((4, "4h"), (24, "24h"), (24 * 7, "7d")):
            doi, dp = pct(oi_now, at_hours_ago(oi, h)), pct(px[-1]["close"], at_hours_ago(px, h))
            parts.append((label, dp, doi))
        print(f"  OPEN INT  {oi_now:,.2f} {prod['underlying_asset']['symbol']} (~${oi_now * mark / 1e6:,.1f}M)  "
              + "  ".join(f"{l}: price {dp:+.1f}% OI {doi:+.1f}%" for l, dp, doi in parts))
        _, dp24, doi24 = parts[1]
        print(f"            last 24h -> {oi_read(dp24, doi24)}")

    # basis
    basis = pct(mark, spot)
    print(f"  BASIS     mark vs spot {basis:+.3f}%  "
          f"({'futures above spot: buyers paying up' if basis > 0.02 else 'futures below spot: sellers pressing' if basis < -0.02 else 'in line with spot'})")

    # order book
    book = d.call("GET", f"/v2/l2orderbook/{sym}", {"depth": 200}, auth=False)
    if book["buy"] and book["sell"]:
        bb, ba = float(book["buy"][0]["price"]), float(book["sell"][0]["price"])
        mid = (bb + ba) / 2
        b05, a05 = (x * cv for x in depth(book, mid, 0.005))
        b1, a1 = (x * cv for x in depth(book, mid, 0.01))
        imb = (b1 - a1) / (b1 + a1) * 100 if b1 + a1 else 0
        s_buy, s_sell = slippage(book["sell"], cv, 10_000), slippage(book["buy"], cv, 10_000)
        sl = lambda s: f"{s:.3f}%" if s is not None else "book too thin"  # noqa: E731
        reach = min(pct(mid, float(book["buy"][-1]["price"])), pct(float(book["sell"][-1]["price"]), mid))
        print(f"  BOOK      spread {pct(ba, bb):.3f}%  depth ±0.5%: bids ${b05 / 1e6:,.1f}M / asks ${a05 / 1e6:,.1f}M  "
              f"±1%: ${b1 / 1e6:,.1f}M / ${a1 / 1e6:,.1f}M  imbalance {imb:+.0f}% "
              f"({'more bids' if imb > 10 else 'more asks' if imb < -10 else 'balanced'})"
              + (f"\n            (API shows the book only to ±{reach:.2f}%, so wider bands are understated)" if reach < 1 else ""))
        print(f"            $10k market order slippage: buy {sl(s_buy)}  sell {sl(s_sell)}")

    # activity
    vol = float(tk.get("turnover_usd") or 0)
    oi_usd = float(tk.get("oi_value_usd") or 0)
    print(f"  ACTIVITY  24h volume ${vol / 1e6:,.0f}M  = {vol / oi_usd if oi_usd else 0:.1f}x open interest")


def main():
    rules = load_rules()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="*", default=rules["watchlist"])
    a = ap.parse_args()
    print(f"Market health {uk_utc()}  (descriptive only, not a signal)")
    d = Delta()
    for sym in (s.upper() for s in a.symbols):
        try:
            analyse(d, sym)
        except (DeltaError, KeyError, ValueError, ZeroDivisionError) as e:
            print(f"\n=== {sym}  ERROR {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
