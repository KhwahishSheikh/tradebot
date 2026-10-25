"""Contracts between layers. Each market plugs in a MarketData + Broker; strategies, risk and
notifiers are market-agnostic. Paper and live brokers share the same interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

import pandas as pd

from tradebot.core.models import (
    Market,
)


class MarketData(ABC):
    market: Market

    @abstractmethod
    def candles(self, symbol: str, resolution: str, start: datetime, end: datetime
                ) -> pd.DataFrame:
        """Bars with open time in [start, end), UTC ts column, sorted."""

    @abstractmethod
    def last_price(self, symbol: str) -> Decimal: ...


class RiskStage(StrEnum):
    PROPOSAL = "proposal"      # when the proposal is created
    EXECUTION = "execution"    # again right before the order is sent


# The risk engine itself lives in tradebot.risk.engine (CryptoRiskEngine.assess).
