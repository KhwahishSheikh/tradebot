"""delta-risk: PASS / BLOCK a proposed trade against rules.json.

  python risk.py BTCUSD buy --entry 80100 --stop 79700 --target 80900 [--contracts 1] [--leverage 3]
  python risk.py ... --balance 50 --open 0 --day-pnl 0     # offline, no API keys

Account numbers (balance, available margin, open positions, today's realised P&L) come from the
API keys in .env (read-only calls) unless given. Exit code 0 = PASS, 2 = BLOCK.
"""
import argparse
import sys
from datetime import datetime, time as dtime

from delta_common import UK, Delta, DeltaError, fmt, now_utc, load_rules
from size import plan


def account_state(d):
    w = d.wallet_usd()
    pos = d.positions()
    start = datetime.combine(now_utc().date(), dtime(0), tzinfo=now_utc().tzinfo)
    pnl = 0.0
    for f in d.fills_since(start):
        np = (f.get("meta_data") or {}).get("new_position") or {}
        pnl += float(np.get("realized_pnl") or 0) if np.get("size") == 0 else 0
        pnl -= float(f.get("commission") or 0)
    return {"balance": float(w.get("balance") or 0), "available": float(w.get("available_balance") or 0),
            "open": len(pos), "symbols": [p["product_symbol"] for p in pos], "day_pnl": pnl}


def check(sym, side, entry, stop, target, contracts, leverage, acct, rules=None, product=None, ticker=None):
    """Returns (verdict, lines). Each line: (status, text) with status PASS/BLOCK/WARN."""
    rules = rules or load_rules()
    d = Delta()
    product = product or d.product(sym)
    ticker = ticker or d.ticker(sym)
    p = plan(sym, side, entry, stop, acct["balance"], target, leverage, rules, product)
    contracts = contracts if contracts is not None else p["contracts"]
    f = lambda x: fmt(x, entry)  # noqa: E731
    out = []

    def add(ok, text, warn=False):
        out.append(("PASS" if ok else ("WARN" if warn else "BLOCK"), text))

    add(sym in rules["allowed_symbols"], f"symbol {sym} in allowed list")
    lev = float(leverage or rules["max_leverage"])
    add(lev <= rules["max_leverage"], f"leverage {lev:g}x <= max {rules['max_leverage']}x")
    good_stop = stop < entry if side == "buy" else stop > entry
    add(good_stop, f"stop {f(stop)} is on the losing side of entry {f(entry)}")
    if target is not None:
        good_tp = target > entry if side == "buy" else target < entry
        add(good_tp, f"target {f(target)} is on the winning side of entry")
        add(p.get("reward_risk", 0) >= rules["min_reward_risk"],
            f"reward:risk {p.get('reward_risk', 0):.2f} >= {rules['min_reward_risk']}")
    else:
        add(False, "take-profit set (required for a bracket order)")
    add(contracts >= 1, f"size {contracts} contract(s) >= 1")
    risk_usd = contracts * p["risk_per_contract_usd"]
    add(risk_usd <= p["risk_budget_usd"] + 1e-9,
        f"risk ${risk_usd:.2f} <= {rules['risk_per_trade_pct']}% of ${acct['balance']:.2f} "
        f"(${p['risk_budget_usd']:.2f})")
    margin = entry * p["contract_value"] * contracts / lev
    add(margin <= acct.get("available", acct["balance"]),
        f"margin ${margin:.2f} <= available ${acct.get('available', acct['balance']):.2f}")
    liq_dist, stop_dist = abs(entry - p["liq_price"]), abs(entry - stop)
    add(liq_dist >= rules["min_liq_buffer_x"] * stop_dist,
        f"liquidation {f(p['liq_price'])} at least {rules['min_liq_buffer_x']:g}x further than stop")
    already = sym in acct.get("symbols", [])
    add(acct["open"] < rules["max_open_positions"] or already,
        f"open positions {acct['open']} < max {rules['max_open_positions']}")
    add(not already, f"no existing {sym} position (don't stack)")
    limit = -acct["balance"] * rules["daily_loss_limit_pct"] / 100
    add(acct["day_pnl"] > limit,
        f"today's P&L ${acct['day_pnl']:.2f} above daily limit ${limit:.2f}")
    # warnings
    fund = float(ticker.get("funding_rate") or 0)
    pays = (side == "buy" and fund > 0) or (side == "sell" and fund < 0)
    now = now_utc()
    next_f = (now.hour // 8 + 1) * 8
    mins = (next_f - now.hour) * 60 - now.minute
    add(not (pays and mins <= rules["funding_blackout_minutes"]),
        f"funding {fund:+.4f}% in {mins} min ({'you pay' if pays else 'you receive'})", warn=True)
    w = rules["trading_window_uk"]
    uk = now.astimezone(UK)
    in_window = uk.strftime("%a") in w["days"] and w["start"] <= uk.strftime("%H:%M") < w["end"]
    add(in_window, f"inside your trading window {w['start']}-{w['end']} UK {'/'.join(w['days'])}", warn=True)
    verdict = "BLOCK" if any(s == "BLOCK" for s, _ in out) else "PASS"
    return verdict, out, p


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbol")
    ap.add_argument("side", choices=["buy", "sell"])
    ap.add_argument("--entry", type=float, help="default: live mark price")
    ap.add_argument("--stop", type=float, required=True)
    ap.add_argument("--target", type=float)
    ap.add_argument("--contracts", type=int, help="default: max allowed by risk rule")
    ap.add_argument("--leverage", type=float)
    ap.add_argument("--balance", type=float)
    ap.add_argument("--available", type=float)
    ap.add_argument("--open", type=int)
    ap.add_argument("--day-pnl", type=float)
    a = ap.parse_args()
    sym = a.symbol.upper()
    entry = a.entry or float(Delta().ticker(sym)["mark_price"])
    if a.balance is not None:
        acct = {"balance": a.balance, "available": a.available if a.available is not None else a.balance,
                "open": a.open or 0, "symbols": [], "day_pnl": a.day_pnl or 0.0}
    else:
        try:
            acct = account_state(Delta(auth=True))
        except DeltaError as e:
            raise SystemExit(f"Pass --balance/--open/--day-pnl (could not read account: {e})")
    verdict, lines, _ = check(sym, a.side, entry, a.stop, a.target, a.contracts, a.leverage, acct)
    for status, text in lines:
        print(f"  [{status:5}] {text}")
    print(f"\nVERDICT: {verdict}")
    sys.exit(0 if verdict == "PASS" else 2)


if __name__ == "__main__":
    main()
