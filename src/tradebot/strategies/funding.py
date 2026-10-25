"""Funding-rate awareness for perpetuals.

Delta quotes funding as % per 8h; positive = longs pay shorts. Two effects on a setup:
  1. cost:     expected funding paid over the holding period, expressed in R
  2. crowding: extreme funding on your side = crowded trade -> confidence penalty
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from tradebot.core.models import Side

CROWDED = 0.03     # % per 8h (~33% annualised): crowded
EXTREME = 0.06     # % per 8h (~66% annualised): very crowded


@dataclass(frozen=True)
class FundingImpact:
    rate_pct_8h: float
    cost_r: float            # +ve = you pay, in units of R
    confidence_delta: float
    note: str


def funding_impact(side: Side, rate_pct_8h: float, entry: float, risk: float,
                   hold_hours: float) -> FundingImpact:
    if rate_pct_8h is None or math.isnan(rate_pct_8h) or risk <= 0:
        return FundingImpact(float("nan"), 0.0, 0.0, "funding n/a")
    sign = 1 if side is Side.BUY else -1
    paid_pct = sign * rate_pct_8h * hold_hours / 8          # % of notional paid over hold
    cost_r = paid_pct / 100 * entry / risk
    on_my_side = sign * rate_pct_8h                         # +ve: my side is the crowded one
    delta = -0.20 if on_my_side >= EXTREME else -0.10 if on_my_side >= CROWDED else (
        0.05 if on_my_side <= -CROWDED else 0.0)            # small bonus for fading the crowd
    who = "longs" if rate_pct_8h > 0 else "shorts"
    note = f"funding {rate_pct_8h:+.4f}%/8h ({who} pay), est. cost {cost_r:+.2f}R over hold"
    if delta < 0:
        note += "; crowded side"
    return FundingImpact(rate_pct_8h, cost_r, delta, note)
