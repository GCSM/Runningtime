"""Deterministic periodization: weekly volume targets that the LLM must stay within.

The LLM decides *what* each workout is; this module decides *how much* running is
safe each week (phases, ~8% ramp, recovery weeks, taper, long-run caps). The skeleton
is recomputed from actual training whenever the plan is reviewed.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, timedelta

DEFAULT_EASY_PACE_S_KM = 390.0  # 6:30/km, used until we've seen real runs
MIN_BASE_KM = 8.0
RAMP = 1.08
RECOVERY_FACTOR = 0.75
TAPER_MULTS = {0: [], 1: [0.55], 2: [0.75, 0.5], 3: [0.85, 0.7, 0.5]}
EXPERIENCE_FACTOR = {"beginner": 0.7, "intermediate": 1.0, "advanced": 1.25}
MIN_WEEKS = {5: 6, 10: 8, 21.1: 10, 42.2: 14}


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _peak_cap(goal_km: float) -> float:
    return 45 if goal_km <= 5.5 else 55 if goal_km <= 10.5 else 70 if goal_km <= 22 else 90


def _long_cap(goal_km: float) -> float:
    return 12 if goal_km <= 5.5 else 16 if goal_km <= 10.5 else 22 if goal_km <= 22 else 32


def _taper_weeks(goal_km: float) -> int:
    return 1 if goal_km <= 10.5 else 2 if goal_km <= 22 else 3


def min_recommended_weeks(goal_km: float) -> int:
    for dist, weeks in MIN_WEEKS.items():
        if goal_km <= dist * 1.05:
            return weeks
    return 16


@dataclass
class Week:
    week_start: date
    phase: str  # base | build | peak | taper | race
    recovery: bool
    race_week: bool
    target_km: float
    long_run_km: float

    def to_row(self) -> dict:
        d = asdict(self)
        d["week_start"] = self.week_start.isoformat()
        d["recovery"] = int(self.recovery)
        d["race_week"] = int(self.race_week)
        return d


def build_skeleton(
    *,
    plan_start: date,
    today: date,
    race_date: date,
    goal_km: float,
    baseline_km: float,
    experience: str,
    weekly_minutes: int,
    long_day_minutes: int,
    easy_pace_s_km: float | None,
) -> list[Week]:
    """Weeks from monday(today) to the race week. Phases are positioned over the whole
    plan (plan_start..race); the ramp starts from `baseline_km` at the current week."""
    start, race_wk = monday(plan_start), monday(race_date)
    n = (race_wk - start).days // 7 + 1
    taper = min(_taper_weeks(goal_km), n)
    n_train = n - taper
    pace_min = (easy_pace_s_km or DEFAULT_EASY_PACE_S_KM) / 60
    cur = max(baseline_km, MIN_BASE_KM)
    cap = max(min(_peak_cap(goal_km) * EXPERIENCE_FACTOR.get(experience, 1.0), weekly_minutes / pace_min), cur)
    long_pct = 0.30 if goal_km <= 10.5 else 0.35
    long_cap = min(_long_cap(goal_km), long_day_minutes / pace_min) if long_day_minutes else _long_cap(goal_km)
    first = max(0, (monday(today) - start).days // 7)

    def long_run(target: float) -> float:
        return round(min(long_pct * target, long_cap) * 2) / 2

    weeks: list[Week] = []
    level = min(cur, cap)
    for i in range(first, n_train):
        recovery = i % 4 == 3 and i != first and i < n_train - 1 and n_train >= 5
        if i != first and not recovery:
            level = min(level * RAMP, cap)
        target = level * RECOVERY_FACTOR if recovery else level
        frac = i / n_train
        phase = "base" if frac < 0.4 else "build" if frac < 0.8 else "peak"
        weeks.append(Week(start + timedelta(weeks=i), phase, recovery, False, round(target * 2) / 2, long_run(target)))
    for k, mult in enumerate(TAPER_MULTS[taper]):
        i = n_train + k
        if i < first:
            continue
        race = i == n - 1
        target = level * mult
        if race:  # the race itself is part of race-week volume; leave room for a short shakeout
            target = math.ceil(max(target, goal_km + 6) * 2) / 2
        weeks.append(
            Week(start + timedelta(weeks=i), "race" if race else "taper", False, race,
                 round(target * 2) / 2, 0.0 if race else long_run(target))
        )
    return weeks
