"""Macro-event blackout windows (FOMC, CPI; RBI/Budget once NSE is enabled)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import yaml


@dataclass(frozen=True)
class MacroEvent:
    name: str
    at: datetime
    markets: tuple[str, ...]
    before: timedelta
    after: timedelta

    def covers(self, market: str, t: datetime) -> bool:
        return market in self.markets and self.at - self.before <= t <= self.at + self.after


class EventCalendar:
    def __init__(self, events: list[MacroEvent]):
        self.events = sorted(events, key=lambda e: e.at)

    @classmethod
    def from_yaml(cls, path: Path | str) -> EventCalendar:
        raw = yaml.safe_load(Path(path).read_text()) or {}
        return cls([MacroEvent(e["name"], datetime.fromisoformat(e["at"]),
                               tuple(e["markets"]), timedelta(minutes=e["before_min"]),
                               timedelta(minutes=e["after_min"])) for e in raw.get("events", [])])

    def active(self, market: str, t: datetime) -> MacroEvent | None:
        return next((e for e in self.events if e.covers(market, t)), None)

    def next_event(self, market: str, t: datetime) -> MacroEvent | None:
        return next((e for e in self.events if market in e.markets and e.at >= t), None)

    def is_stale(self, t: datetime, horizon_days: int = 30) -> bool:
        """True if nothing is listed in the next `horizon_days`: someone forgot to update the file."""
        return not any(t <= e.at <= t + timedelta(days=horizon_days) for e in self.events)
