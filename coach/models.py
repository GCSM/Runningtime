from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WORKOUT_TYPES = ["easy", "long", "tempo", "intervals", "recovery", "race", "cross"]
KM_PER_MI = 1.609344


class DayAvail(BaseModel):
    available: bool = False
    minutes: int = Field(default=0, ge=0, le=300)  # time available for a run that day
    time: str = ""  # typical start time, free text, e.g. "06:00"


class Profile(BaseModel):
    name: str = ""
    goal_label: str = "Race"  # "5K", "Half marathon", ...
    goal_km: float = Field(gt=0)
    race_date: date
    goal_time_s: int | None = None  # optional target finish time
    baseline_km: float = 0.0  # self-reported current weekly km (used until Strava data exists)
    experience: Literal["beginner", "intermediate", "advanced"] = "intermediate"
    availability: dict[str, DayAvail]
    long_run_day: str = "sun"
    notes: str = ""  # injuries, preferences, constraints
    units: Literal["km", "mi"] = "km"

    @property
    def goal_pace_s_per_km(self) -> float | None:
        return self.goal_time_s / self.goal_km if self.goal_time_s else None

    @property
    def weekly_minutes(self) -> int:
        return sum(a.minutes for a in self.availability.values() if a.available)

    @property
    def long_day_minutes(self) -> int:
        a = self.availability.get(self.long_run_day)
        return a.minutes if a and a.available else 0
