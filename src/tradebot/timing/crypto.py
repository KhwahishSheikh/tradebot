"""Crypto (Delta) timing: 24/7 market, funding timestamps, low-liquidity windows. All times UTC."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tradebot.config import CryptoTimingConfig
from tradebot.timing.windows import hhmm_to_minutes


class CryptoClock:
    def __init__(self, cfg: CryptoTimingConfig):
        self.cfg = cfg
        self._interval = timedelta(hours=cfg.funding_interval_hours)
        self._anchor_min = hhmm_to_minutes(cfg.funding_anchor_utc)

    def _anchor(self, at: datetime) -> datetime:
        day = at.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return day + timedelta(minutes=self._anchor_min) - timedelta(days=1)

    def next_funding(self, at: datetime) -> datetime:
        """First funding timestamp strictly after `at`."""
        at = at.astimezone(UTC)
        t = self._anchor(at)
        while t <= at:
            t += self._interval
        return t

    def prev_funding(self, at: datetime) -> datetime:
        return self.next_funding(at) - self._interval

    def in_funding_blackout(self, at: datetime) -> bool:
        window = timedelta(minutes=self.cfg.funding_blackout_minutes)
        return (self.next_funding(at) - at <= window) or (at - self.prev_funding(at) < window)

    def is_low_liquidity(self, at: datetime) -> bool:
        local = at.astimezone(UTC)
        return any(w.contains(local) for w in self.cfg.low_liquidity_utc)
