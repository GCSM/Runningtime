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
    longest_recent_km: float = 0.0,
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
    freq_cap = K.frequency_cap_km(experience, goal_km, run_days, pace_s)
    cap = max(min(target, time_cap, freq_cap), cur)    # never push volume below what they already do; time and run frequency are hard ceilings
    first = max(0, (monday(today) - start).days // 7)

    recent_longs: list[float] = [longest_recent_km] if longest_recent_km > 0 else []

    def long_run(week_km: float, grow: bool = True, recovery: bool = False) -> float:
        """The long run for a week: its share/size/time limits, and growth of at most +10% or +2.5 km on the longest of the last four weeks.
        With no history the first long run is capped at 40% of the week, so a short week can't open with a huge long run."""
        raw = K.long_run_km(week_km, goal_km, experience, run_days, long_day_minutes, pace_s)
        if grow:
            prior = max(recent_longs[-4:], default=0.0)
            allowed = max(prior * (1 + K.LONG_RUN_GROWTH_PCT), prior + K.LONG_RUN_GROWTH_FLOOR_KM) if prior > 0 else 0.40 * week_km
            raw = min(raw, math.floor(allowed * 2) / 2)
            if recovery and prior > 0:                       # a recovery week cuts the long run too, not just the weekday runs
                raw = min(raw, math.floor(K.RECOVERY_FACTOR * prior * 2) / 2)
        recent_longs.append(raw)
        return raw

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
        weeks.append(Week(start + timedelta(weeks=i), phase, recovery, False, round(week_km * 2) / 2, long_run(week_km, recovery=recovery)))

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
        limit = "goal" if min(time_cap, freq_cap) >= target - 0.5 else ("frequency" if freq_cap <= time_cap else "time")
    else:
        limit = "ramp"
    peak_long = max((w.long_run_km for w in training), default=0.0)
    wtr = max(0, (race_date - today).days // 7)
    outlook = VolumeOutlook(
        baseline_km=baseline_km, baseline_source=baseline_source, start_km=weeks[0].target_km if weeks else cur, peak_km=peak,
        ideal_low=low, ideal_target=target, ideal_high=high, limit=limit, time_cap_km=time_cap,
        weeks_to_race=wtr, min_weeks=K.min_weeks(goal_km, experience), peak_long_km=peak_long, freq_cap_km=freq_cap, run_days=run_days,
    )
    outlook.warnings = K.readiness_warnings(
        goal_km, experience, peak_km=peak, peak_long_km=peak_long, limit=limit, time_cap_km=time_cap, long_day_minutes=long_day_minutes,
        weekly_minutes=weekly_minutes, pace_s_km=pace_s, weeks_to_race=wtr, min_weeks_=outlook.min_weeks, ideal_low=low)
    return weeks, outlook


def build_skeleton(**kw) -> list[Week]:
    return build_skeleton_info(**kw)[0]
