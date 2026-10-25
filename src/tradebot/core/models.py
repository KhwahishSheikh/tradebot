"""Domain objects shared by every module. Prices and sizes are Decimal: no float rounding in orders."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4


class Market(StrEnum):
    CRYPTO_DELTA = "crypto_delta"
    NSE = "nse"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Horizon(StrEnum):
    INTRADAY = "intraday"
    SWING = "swing"
    LONG_TERM = "long_term"


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"
    STOP_LOSS = "stop_loss"          # SL (limit)
    STOP_LOSS_MARKET = "stop_loss_market"  # SL-M


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    DRIFT_CANCELLED = "drift_cancelled"
    RISK_BLOCKED = "risk_blocked"
    EXECUTED = "executed"


@dataclass(frozen=True)
class Instrument:
    market: Market
    symbol: str                  # "BTCUSD" / "RELIANCE-EQ" / "NIFTY25OCT25000CE"
    exchange_token: str | None = None   # Delta product_id / Angel symboltoken
    lot_size: Decimal = Decimal(1)
    tick_size: Decimal = Decimal("0.05")


@dataclass(frozen=True)
class Signal:
    """Raw strategy output, before sizing or risk checks."""

    instrument: Instrument
    side: Side
    strategy: str
    horizon: Horizon
    entry: Decimal
    stop_loss: Decimal
    target: Decimal
    confidence: float            # 0..1
    reasoning: str
    created_at: datetime
    timeframe: str = ""
    regime: str = ""
    max_hold: timedelta | None = None
    features: dict = field(default_factory=dict, hash=False, compare=False)
    id: str = field(default_factory=lambda: uuid4().hex[:12])

    @property
    def risk_reward(self) -> float:
        risk = abs(self.entry - self.stop_loss)
        return float(abs(self.target - self.entry) / risk) if risk else 0.0
