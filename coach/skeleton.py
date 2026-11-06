"""Deterministic periodization: weekly volume targets that the language model must stay within.

The model decides *what* each workout is; this module decides *how much* running each week holds, using the researched
numbers in coach/knowledge.py (docs/RESEARCH.md): published peak volumes by goal and level, a level-based ramp, a
recovery week every 4th week, a distance-dependent taper, and long-run caps. It is recomputed from actual training
whenever the plan is reviewed.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from . import knowledge as K

DEFAULT_EASY_PACE_S_KM = 390.0  # 6:30/km, used until we know the athlete's real pace
MIN_BASE_KM = K.MIN_BASE_KM
VolumeOutlook = K.VolumeOutlook


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def min_recommended_weeks(goal_km: float, level: str = "intermediate") -> int:
    return K.min_weeks(goal_km, level)


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


def build_skeleton_info(
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
    run_days: int = 4,
    baseline_source: str = "stated",
) -> tuple[list[Week], VolumeOutlook]:
    """Weeks from monday(today) to the race week, plus a description of the volume plan. Phases are positioned over the
    whole plan (plan_start..race); the ramp starts from `baseline_km` at the current week."""
    start, race_wk = monday(plan_start), monday(race_date)
    n = (race_wk - start).days // 7 + 1
    mults = K.taper_multipliers(goal_km)
    taper = min(len(mults), n)
    mults = mults[len(mults) - taper:] if taper < len(mults) else mults
    n_train = n - taper

    pace_s = easy_pace_s_km or DEFAULT_EASY_PACE_S_KM
    low, target, high = K.peak_range(goal_km, experience)
    time_cap = weekly_minutes / (pace_s / 60)
    cur = max(baseline_km, MIN_BASE_KM)
    cap = max(min(target, time_cap), cur)        # never push volume below what they already do; time is a hard ceiling otherwise
    first = max(0, (monday(today) - start).days // 7)

    def long_run(week_km: float) -> float:
        return K.long_run_km(week_km, goal_km, experience, run_days, long_day_minutes, pace_s)

    weeks: list[Week] = []
    level = min(cur, cap)
    for i in range(first, n_train):
        recovery = (i % K.RECOVERY_EVERY == K.RECOVERY_EVERY - 1 and i != first and i < n_train - 1
                    and n_train >= K.MIN_WEEKS_FOR_RECOVERY)
        if i != first and not recovery:
            level = min(K.next_week_km(level, experience), cap)
        week_km = level * K.RECOVERY_FACTOR if recovery else level
        frac = i / n_train
        phase = "base" if frac < 0.4 else "build" if frac < 0.8 else "peak"
        weeks.append(Week(start + timedelta(weeks=i), phase, recovery, False, round(week_km * 2) / 2, long_run(week_km)))

    for k, mult in enumerate(mults):
        i = n_train + k
        if i < first:
            continue
        race = i == n - 1
        week_km = level * mult
        if race:  # the race itself is part of race-week volume; leave room for a short shakeout
            week_km = math.ceil(max(week_km, goal_km + 6) * 2) / 2
        weeks.append(Week(start + timedelta(weeks=i), "race" if race else "taper", False, race,
                          round(week_km * 2) / 2, 0.0 if race else long_run(week_km)))

    training = [w for w in weeks if w.phase in ("base", "build", "peak")]
    peak = max((w.target_km for w in training), default=round(cur * 2) / 2)
    if not training or cur >= target:
        limit = "none"
    elif level >= cap - 0.25:
        limit = "time" if time_cap < target - 0.5 else "goal"
    else:
        limit = "ramp"
    peak_long = max((w.long_run_km for w in training), default=0.0)
    wtr = max(0, (race_date - today).days // 7)
    outlook = VolumeOutlook(
        baseline_km=baseline_km, baseline_source=baseline_source, start_km=weeks[0].target_km if weeks else cur, peak_km=peak,
        ideal_low=low, ideal_target=target, ideal_high=high, limit=limit, time_cap_km=time_cap,
        weeks_to_race=wtr, min_weeks=K.min_weeks(goal_km, experience), peak_long_km=peak_long,
    )
    outlook.warnings = K.readiness_warnings(
        goal_km, experience, peak_km=peak, peak_long_km=peak_long, limit=limit, time_cap_km=time_cap, long_day_minutes=long_day_minutes,
        weekly_minutes=weekly_minutes, pace_s_km=pace_s, weeks_to_race=wtr, min_weeks_=outlook.min_weeks, ideal_low=low)
    return weeks, outlook


def build_skeleton(**kw) -> list[Week]:
    return build_skeleton_info(**kw)[0]
