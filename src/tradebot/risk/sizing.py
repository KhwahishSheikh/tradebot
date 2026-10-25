"""Contract specs and position sizing for Delta linear (vanilla) USD perpetuals.

PnL per contract = contract_value x (exit - entry) USD. Size comes from the stop distance:
    contracts = floor(risk_budget_usd / (|entry - stop| x contract_value))
then capped by leverage, gross exposure and portfolio heat. Never rounded UP.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from tradebot.core.models import Side


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    product_id: int
    contract_value: float          # underlying units per contract (BTCUSD: 0.001 BTC)
    tick_size: float
    maintenance_margin_pct: float  # e.g. 0.25 (%)
    position_size_limit: int


def load_specs(delta, symbols: list[str], cache: Path | str, max_age_hours: float = 24
               ) -> dict[str, ContractSpec]:
    """From Delta's /v2/products, cached to disk (specs change rarely)."""
    cache = Path(cache)
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age_hours * 3600:
        raw = json.loads(cache.read_text())
        if set(symbols) <= set(raw):
            return {s: ContractSpec(**raw[s]) for s in symbols}
    specs = {}
    for s in symbols:
        p = delta.product(s)
        specs[s] = ContractSpec(s, int(p["id"]), float(p["contract_value"]), float(p["tick_size"]),
                                float(p["maintenance_margin"]), int(p["position_size_limit"]))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({s: asdict(v) for s, v in specs.items()}, indent=1))
    return specs


def liquidation_price(side: Side, entry: float, leverage: float, mm_pct: float) -> float:
    """Isolated-margin approximation (ignores fees/funding, so the real one is slightly closer)."""
    if side is Side.BUY:
        return entry * (1 - 1 / leverage + mm_pct / 100)
    return entry * (1 + 1 / leverage - mm_pct / 100)


@dataclass(frozen=True)
class Sizing:
    contracts: int
    entry: float
    stop: float
    notional_usd: float
    risk_usd: float
    risk_inr: float
    risk_pct: float            # of equity
    order_leverage: float      # leverage to SET on the exchange before ordering
    effective_leverage: float  # notional / equity
    margin_usd: float
    liq_price: float
    limited_by: str            # which constraint set the size

    def line(self) -> str:
        return (f"{self.contracts} contracts, notional ${self.notional_usd:,.0f} "
                f"({self.effective_leverage:.2f}x equity), risk ₹{self.risk_inr:,.0f} "
                f"({self.risk_pct:.2f}%), set {self.order_leverage:g}x, liq ~{self.liq_price:,.1f}")


def size_position(spec: ContractSpec, side: Side, entry: float, stop: float, *,
                  equity_inr: float, usd_inr: float, risk_pct: float, order_leverage: float,
                  max_notional_usd: float, max_risk_usd: float,
                  round_trip_cost_pct: float = 0.0) -> Sizing | str:
    """Returns a Sizing, or a string saying why no size is possible.
    round_trip_cost_pct (fees+GST+slippage, both sides) is added to the stop distance so that a
    stop-out costs at most the risk budget all-in, not budget + costs."""
    per_contract_risk = (abs(entry - stop) + entry * round_trip_cost_pct / 100) * spec.contract_value
    if per_contract_risk <= 0:
        return "stop equals entry"
    equity_usd = equity_inr / usd_inr
    budget_usd = equity_usd * risk_pct / 100
    caps = {
        "risk per trade": budget_usd / per_contract_risk,
        "portfolio heat": max_risk_usd / per_contract_risk,
        "gross leverage": max_notional_usd / (entry * spec.contract_value),
        "exchange size limit": spec.position_size_limit,
    }
    limited_by = min(caps, key=caps.get)
    contracts = math.floor(max(0.0, caps[limited_by]))
    if contracts < 1:
        one_risk_pct = per_contract_risk / equity_usd * 100
        return (f"0 contracts (limited by {limited_by}); one contract would risk "
                f"{one_risk_pct:.2f}% of equity")
    notional = contracts * spec.contract_value * entry
    risk_usd = contracts * per_contract_risk
    return Sizing(
        contracts=contracts, entry=entry, stop=stop, notional_usd=notional, risk_usd=risk_usd,
        risk_inr=risk_usd * usd_inr, risk_pct=risk_usd / equity_usd * 100,
        order_leverage=order_leverage, effective_leverage=notional / equity_usd,
        margin_usd=notional / order_leverage,
        liq_price=liquidation_price(side, entry, order_leverage, spec.maintenance_margin_pct),
        limited_by=limited_by)
