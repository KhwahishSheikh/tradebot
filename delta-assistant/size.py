"""delta-budget: how many contracts fit your risk rule.

  python size.py BTCUSD buy --entry 80100 --stop 79700 [--target 80900] [--balance 50]

Without --balance it reads your wallet with the API keys in .env (read-only call).
Risk = (distance to stop + taker fees both ways) x contracts. Never sizes above rules.json.
"""
import argparse
import math

from delta_common import Delta, DeltaError, fmt, liq_price, load_rules


def plan(sym, side, entry, stop, balance, target=None, leverage=None, rules=None, product=None):
    rules = rules or load_rules()
    if (side == "buy" and stop >= entry) or (side == "sell" and stop <= entry):
        raise SystemExit(f"Stop {stop} must be {'below' if side == 'buy' else 'above'} entry {entry} for a {side}.")
    product = product or Delta().product(sym)
    cv = float(product["contract_value"])
    mm_pct = float(product.get("maintenance_margin") or 0.5)
    lev = min(float(leverage or rules["max_leverage"]), float(rules["max_leverage"]))
    fee = rules["taker_fee_pct"] / 100
    stop_dist = abs(entry - stop)
    per_contract = stop_dist * cv + entry * cv * fee * 2
    risk_budget = balance * rules["risk_per_trade_pct"] / 100
    contracts = math.floor(risk_budget / per_contract) if per_contract > 0 else 0
    notional = entry * cv * max(contracts, 1)
    out = {
        "symbol": sym, "side": side, "entry": entry, "stop": stop, "target": target,
        "contract_value": cv, "leverage": lev, "balance": balance,
        "risk_budget_usd": risk_budget, "risk_per_contract_usd": per_contract,
        "contracts": contracts, "risk_usd": contracts * per_contract,
        "margin_usd": notional / lev, "notional_usd": notional,
        "liq_price": liq_price(entry, side, lev, mm_pct),
        "stop_pct": stop_dist / entry * 100,
        "min_balance_for_1": per_contract / (rules["risk_per_trade_pct"] / 100),
    }
    if target:
        reward = abs(target - entry) * cv - entry * cv * fee * 2
        out["reward_risk"] = reward / per_contract if per_contract else 0
    return out


def show(p):
    f = lambda x: fmt(x, p["entry"])  # noqa: E731
    print(f"{p['symbol']} {p['side'].upper()}  entry {f(p['entry'])}  stop {f(p['stop'])} "
          f"({p['stop_pct']:.2f}%)" + (f"  target {f(p['target'])}" if p.get("target") else ""))
    print(f"  balance ${p['balance']:.2f} -> risk budget ${p['risk_budget_usd']:.2f}  "
          f"| 1 contract risks ${p['risk_per_contract_usd']:.2f} (incl. fees)")
    if p["contracts"] < 1:
        print(f"  CONTRACTS: 0  -> too small. 1 contract needs a balance of about "
              f"${p['min_balance_for_1']:.2f} at this stop.")
    else:
        print(f"  CONTRACTS: {p['contracts']}  risk ${p['risk_usd']:.2f}  "
              f"margin ${p['margin_usd']:.2f} @{p['leverage']:g}x  notional ${p['notional_usd']:.2f}")
    print(f"  est. liquidation {f(p['liq_price'])} (isolated worst case)"
          + (f"  | reward:risk {p['reward_risk']:.2f}" if "reward_risk" in p else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbol")
    ap.add_argument("side", choices=["buy", "sell"])
    ap.add_argument("--entry", type=float, help="default: live mark price")
    ap.add_argument("--stop", type=float, required=True)
    ap.add_argument("--target", type=float)
    ap.add_argument("--balance", type=float)
    ap.add_argument("--leverage", type=float)
    a = ap.parse_args()
    sym = a.symbol.upper()
    entry = a.entry or float(Delta().ticker(sym)["mark_price"])
    balance = a.balance
    if balance is None:
        try:
            balance = float(Delta(auth=True).wallet_usd().get("balance") or 0)
        except DeltaError as e:
            raise SystemExit(f"Pass --balance (could not read wallet: {e})")
    show(plan(sym, a.side, entry, a.stop, balance, a.target, a.leverage))


if __name__ == "__main__":
    main()
