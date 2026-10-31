"""delta-broker: the ONLY script that changes anything on Delta. Dry-run unless --confirm.

  python trade.py status
  python trade.py buy  BTCUSD --contracts 1 --stop 79700 --target 80900 [--limit 80000] [--leverage 3]
  python trade.py sell ETHUSD --contracts 1 --stop 2350  --target 2260
  python trade.py protect XAUTUSD --stop 4040 [--target 3990]   # add SL/TP to an open position
  python trade.py close   XAUTUSD                                # reduce-only market close
  python trade.py cancel  BTCUSD                                 # cancel open orders on a symbol

Every entry: risk.py must PASS, leverage is set first, and the stop-loss + take-profit ride on the
same order (bracket), so the exchange protects the position even if your Mac is off.
Add --confirm to send. LIVE=true in .env means REAL MONEY; Claude never adds --confirm on live.
"""
import argparse
import sys
import time
import uuid
from datetime import datetime, timezone

from delta_common import Delta, DeltaError, HERE, fmt, is_live, round_tick
from risk import account_state, check

JOURNAL = HERE / "journal.csv"
HEADER = ("date_utc,symbol,side,strategy,entry,stop_loss,take_profit,contracts,leverage,risk_usd,"
          "risk_pct,exit_price,pnl_usd,fees_usd,r_multiple,mode,order_id,notes\n")


def banner():
    print("MODE: " + ("LIVE — REAL MONEY" if is_live() else "TESTNET (fake money)"))


def journal(row):
    if not JOURNAL.exists():
        JOURNAL.write_text(HEADER)
    with JOURNAL.open("a") as fh:
        fh.write(",".join("" if v is None else str(v).replace(",", ";") for v in row) + "\n")


def status(d):
    w = d.wallet_usd()
    print(f"wallet ${float(w.get('balance') or 0):.2f}  available ${float(w.get('available_balance') or 0):.2f}")
    pos = d.positions()
    if not pos:
        print("no open positions")
    for p in pos:
        orders = d.open_orders(p["product_id"])
        sl = [o for o in orders if o.get("stop_order_type") == "stop_loss_order"]
        tp = [o for o in orders if o.get("stop_order_type") == "take_profit_order"]
        size = int(p["size"])
        print(f"{p['product_symbol']}: {'LONG' if size > 0 else 'SHORT'} {abs(size)}  entry {p['entry_price']}  "
              f"mark {p.get('mark_price')}  uPnL ${float(p.get('unrealized_pnl') or 0):.2f}  "
              f"liq {p.get('liquidation_price')}  SL {sl[0]['stop_price'] if sl else 'NONE ⚠'}  "
              f"TP {tp[0]['stop_price'] if tp else 'none'}")


def entry(d, a, side):
    sym = a.symbol.upper()
    pub = Delta()
    product, ticker = pub.product(sym), pub.ticker(sym)
    tick = float(product["tick_size"])
    px = a.limit or float(ticker["mark_price"])
    stop, target = round_tick(a.stop, tick), round_tick(a.target, tick)
    acct = account_state(d)
    verdict, lines, p = check(sym, side, px, stop, target, a.contracts, a.leverage, acct,
                              product=product, ticker=ticker)
    f = lambda x: fmt(x, px)  # noqa: E731
    print(f"\nTICKET  {side.upper()} {a.contracts} {sym}  {'limit ' + f(a.limit) if a.limit else 'market ~' + f(px)}"
          f"  SL {f(stop)}  TP {f(target)}  leverage {a.leverage:g}x")
    for s, t in lines:
        print(f"  [{s:5}] {t}")
    print(f"VERDICT: {verdict}")
    if verdict != "PASS":
        sys.exit("Blocked by your rules — nothing sent.")
    if not a.confirm:
        print("\nDRY RUN — nothing sent. Add --confirm to place it.")
        return
    lev = d.call("POST", f"/v2/products/{product['id']}/orders/leverage", body={"leverage": f"{a.leverage:g}"})
    print(f"leverage set: {lev.get('leverage')}x")
    body = {"product_id": product["id"], "size": a.contracts, "side": side,
            "order_type": "limit_order" if a.limit else "market_order",
            "client_order_id": f"da-{uuid.uuid4().hex[:16]}",
            "bracket_stop_loss_price": f"{stop}",
            "bracket_take_profit_price": f"{target}", "bracket_take_profit_limit_price": f"{target}",
            "bracket_stop_trigger_method": "last_traded_price"}
    if a.limit:
        body["limit_price"] = f"{round_tick(a.limit, tick)}"
    o = d.call("POST", "/v2/orders", body=body)
    print(f"order {o.get('id')} state {o.get('state')} filled {o.get('size', 0) - (o.get('unfilled_size') or 0)}")
    if not a.limit:  # market: make sure the stop really sits on the exchange
        time.sleep(2)
        if not any(x.get("stop_order_type") == "stop_loss_order" for x in d.open_orders(product["id"])):
            protect_position(d, product, stop, target)
    journal([datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), sym, side, a.strategy, px, stop, target,
             a.contracts, a.leverage, f"{p['risk_per_contract_usd'] * a.contracts:.4f}",
             f"{p['risk_per_contract_usd'] * a.contracts / acct['balance'] * 100:.2f}" if acct["balance"] else "",
             "", "", "", "", "live" if is_live() else "testnet", o.get("id"), "trade.py bracket"])
    print("journal.csv updated. Check with: python trade.py status")


def protect_position(d, product, stop, target):
    body = {"product_id": product["id"],
            "stop_loss_order": {"order_type": "market_order", "stop_price": f"{stop}"},
            "bracket_stop_trigger_method": "last_traded_price"}
    if target is not None:
        body["take_profit_order"] = {"order_type": "limit_order", "stop_price": f"{target}",
                                     "limit_price": f"{target}"}
    d.call("POST", "/v2/orders/bracket", body=body)
    print(f"protection placed: SL {stop}" + (f"  TP {target}" if target is not None else ""))


def find_position(d, sym):
    for p in d.positions():
        if p["product_symbol"] == sym:
            return p
    sys.exit(f"No open {sym} position.")


def protect(d, a):
    sym = a.symbol.upper()
    p = find_position(d, sym)
    product = Delta().product(sym)
    tick = float(product["tick_size"])
    size, mark = int(p["size"]), float(p["mark_price"])
    stop = round_tick(a.stop, tick)
    target = round_tick(a.target, tick) if a.target else None
    long = size > 0
    if (long and stop >= mark) or (not long and stop <= mark):
        sys.exit(f"Stop {stop} would trigger immediately (mark {mark}). For a {'long' if long else 'short'} it "
                 f"must be {'below' if long else 'above'} the price.")
    loss = abs(float(p["entry_price"]) - stop) * float(product["contract_value"]) * abs(size)
    locked = (stop - float(p["entry_price"])) * (1 if long else -1) * float(product["contract_value"]) * abs(size)
    print(f"{sym} {'LONG' if long else 'SHORT'} {abs(size)} entry {p['entry_price']} mark {mark}")
    print(f"  new SL {stop}" + (f"  TP {target}" if target else "") +
          (f"  -> locks in about ${locked:.2f} profit" if locked > 0 else f"  -> max loss about ${loss:.2f}"))
    if not a.confirm:
        print("DRY RUN — nothing sent. Add --confirm to place it.")
        return
    protect_position(d, product, stop, target)


def close(d, a):
    sym = a.symbol.upper()
    p = find_position(d, sym)
    size = int(p["size"])
    print(f"close {sym}: {'SELL' if size > 0 else 'BUY'} {abs(size)} reduce-only market  (uPnL ${float(p.get('unrealized_pnl') or 0):.2f})")
    if not a.confirm:
        print("DRY RUN — nothing sent. Add --confirm to close it.")
        return
    o = d.call("POST", "/v2/orders", body={"product_id": p["product_id"], "size": abs(size),
                                           "side": "sell" if size > 0 else "buy",
                                           "order_type": "market_order", "reduce_only": True})
    print(f"close order {o.get('id')} state {o.get('state')}")
    d.call("DELETE", "/v2/orders/all", body={"product_id": p["product_id"]})
    print("left-over SL/TP orders cancelled")


def cancel(d, a):
    product = Delta().product(a.symbol.upper())
    orders = d.open_orders(product["id"])
    print(f"{len(orders)} open order(s) on {a.symbol.upper()}")
    if orders and not a.confirm:
        print("DRY RUN — nothing sent. Add --confirm to cancel them.")
        return
    if orders:
        d.call("DELETE", "/v2/orders/all", body={"product_id": product["id"]})
        print("cancelled")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    for side in ("buy", "sell"):
        s = sub.add_parser(side)
        s.add_argument("symbol")
        s.add_argument("--contracts", type=int, required=True)
        s.add_argument("--stop", type=float, required=True)
        s.add_argument("--target", type=float, required=True)
        s.add_argument("--limit", type=float, help="limit price; default market")
        s.add_argument("--leverage", type=float, default=3)
        s.add_argument("--strategy", default="manual")
        s.add_argument("--confirm", action="store_true")
    s = sub.add_parser("protect")
    s.add_argument("symbol")
    s.add_argument("--stop", type=float, required=True)
    s.add_argument("--target", type=float)
    s.add_argument("--confirm", action="store_true")
    for name in ("close", "cancel"):
        s = sub.add_parser(name)
        s.add_argument("symbol")
        s.add_argument("--confirm", action="store_true")
    a = ap.parse_args()
    banner()
    try:
        d = Delta(auth=True)
        if a.cmd == "status":
            status(d)
        elif a.cmd in ("buy", "sell"):
            entry(d, a, a.cmd)
        else:
            {"protect": protect, "close": close, "cancel": cancel}[a.cmd](d, a)
    except DeltaError as e:
        sys.exit(f"Delta error: {e}\nNothing retried. Check `python trade.py status` before trying again.")


if __name__ == "__main__":
    main()
