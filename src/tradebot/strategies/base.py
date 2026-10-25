"""Strategy plug-in contract.

A strategy is two pure functions over the shared feature frame:
  prepare(f)    -> add its own causal columns (vectorised, run once)
  decide(f, i)  -> look at rows <= i only, return a Setup or None
The same code path serves live signals (i = last closed bar) and research (every i).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pandas as pd

from tradebot.core.models import Horizon, Side
from tradebot.strategies.features import Regime


@dataclass(frozen=True)
class Setup:
    """Strategy output before funding adjustment, regime gating and risk sizing."""

    side: Side
    entry: float
    stop: float
    target: float
    confidence: float           # strategy's own 0..1 score
    reasons: tuple[str, ...]
    features: dict[str, Any] = field(default_factory=dict)

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def rr(self) -> float:
        return abs(self.target - self.entry) / self.risk if self.risk else 0.0


class Strategy(ABC):
    name: ClassVar[str]
    horizon: ClassVar[Horizon]
    regimes: ClassVar[frozenset[Regime]]      # regimes this strategy is allowed to trade in
    defaults: ClassVar[dict[str, Any]] = {}
    warmup: ClassVar[int] = 250               # bars before decide() is meaningful

    def __init__(self, timeframe: str = "1h", max_hold_bars: int = 48, **params: Any):
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"{self.name}: unknown params {sorted(unknown)}")
        self.timeframe = timeframe
        self.max_hold_bars = max_hold_bars
        self.p = {**self.defaults, **params}

    def prepare(self, f: pd.DataFrame) -> pd.DataFrame:
        return f

    @abstractmethod
    def decide(self, f: pd.DataFrame, i: int) -> Setup | None: ...

    # helpers shared by strategies
    def _bracket(self, side: Side, entry: float, atr: float) -> tuple[float, float]:
        risk = self.p["stop_atr"] * atr
        stop = entry - risk if side is Side.BUY else entry + risk
        target = entry + self.p["rr"] * risk if side is Side.BUY else entry - self.p["rr"] * risk
        return stop, target


def clip01(x: float) -> float:
    return max(0.0, min(1.0, x))
