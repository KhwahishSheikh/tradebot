"""Weekly recurring time windows, e.g. "mon-fri 09:00-22:00" or overnight "fri 22:00-02:00"."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class Weekday(StrEnum):
    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"

    @property
    def index(self) -> int:
        return list(Weekday).index(self)


WEEKDAYS = [Weekday.MON, Weekday.TUE, Weekday.WED, Weekday.THU, Weekday.FRI]
ALL_DAYS = list(Weekday)


def hhmm_to_minutes(v: str) -> int:
    """'09:15' -> 555. '24:00' is allowed as end-of-day."""
    h, m = (int(x) for x in v.split(":"))
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m):
        raise ValueError(f"bad time {v!r}, expected HH:MM")
    return h * 60 + m


class TimeWindow(BaseModel):
    """`days` are the days the window STARTS on. If end <= start the window crosses midnight."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    days: list[Weekday]
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def _valid(cls, v: str) -> str:
        hhmm_to_minutes(v)
        return v

    def contains(self, local: datetime) -> bool:
        """`local` must already be in the timezone the window is defined in."""
        s, e = hhmm_to_minutes(self.start), hhmm_to_minutes(self.end)
        m, wd = local.hour * 60 + local.minute, local.weekday()
        day_idx = {d.index for d in self.days}
        if s < e:
            return wd in day_idx and s <= m < e
        return (wd in day_idx and m >= s) or ((wd - 1) % 7 in day_idx and m < e)
