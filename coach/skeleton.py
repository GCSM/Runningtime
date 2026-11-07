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
    other_day_minutes: list[int] | None = None,
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
    # What the athlete's days can really carry (not just minutes / pace: a 4-hour long-run day can't hold more than the long-run caps allow)
    time_cap = (K.run_capacity_km(experience, goal_km, other_day_minutes, long_day_minutes, pace_s) if other_day_minutes is not None
                else weekly_minutes / (pace_s / 60))
    cur = max(baseline_km, MIN_BASE_KM)
    freq_cap = K.frequency_cap_km(experience, goal_km, run_days, pace_s)
    ceiling = min(time_cap, freq_cap)                  # what their days and run frequency can carry
    # Aim for the published target; an athlete already above it keeps their volume (up to 25% over the top of the range), but never more than
    # their days can carry: a plan that targets more than can be run only under-delivers silently.
    cap = min(ceiling, max(target, min(cur, high * 1.25)))
    cap = max(cap, MIN_BASE_KM)
    first = max(0, (monday(today) - start).days // 7)

    recent_longs: list[float] = [longest_recent_km] if longest_recent_km > 0 else []

    def long_run(week_km: float, ladder: float | None = None, recovery: bool = False) -> float:
        """The long run for a week: its share/size/time limits, growth of at most +10% or +2.5 km on the longest of the last four weeks,
        and the build-up ladder for this week. With no history the first long run is capped at 40% of the week."""
        raw = K.long_run_km(week_km, goal_km, experience, run_days, long_day_minutes, pace_s)
        prior = max(recent_longs[-4:], default=0.0)
        allowed = max(prior * (1 + K.LONG_RUN_GROWTH_PCT), prior + K.LONG_RUN_GROWTH_FLOOR_KM) if prior > 0 else 0.40 * week_km
        raw = min(raw, math.floor(allowed * 2) / 2)
        if ladder is not None:
            raw = min(raw, math.floor(ladder * 2) / 2)
        if recovery and prior > 0:                           # a recovery week cuts the long run too, not just the weekday runs
            raw = min(raw, math.floor(K.RECOVERY_FACTOR * prior * 2) / 2)
        recent_longs.append(raw)
        return raw

    # ---- the build: volume and long run climb gradually across ALL the build weeks and arrive at their peak near the end -------------
    # (Published plans don't reach their peak early and hold it: the biggest long run comes once or twice, in the last weeks before the
    # taper. The ramp is the gentler of "as fast as is safe" and "evenly spread so the peak lands at the end".)
    train = list(range(first, n_train))
    # Step-back weeks every fourth week, counted BACK from the end so the plan finishes with its peak block (two build weeks) before the taper,
    # not a step-back right before the biggest weeks. Absolute week indexes, not 'relative to the current week': a review on the Monday of a
    # step-back week must keep it a step-back week, and the schedule doesn't shift from one review to the next.
    recov = {i for i in train if n_train >= K.MIN_WEEKS_FOR_RECOVERY and i >= 2 and i <= n_train - 3 and (n_train - 3 - i) % K.RECOVERY_EVERY == 0}
    m = sum(1 for i in train if i not in recov)                  # number of build (non-recovery) weeks from here
    mid = first > 0                                              # a mid-plan rebuild: the baseline is what was ALREADY achieved, so this week steps up from it
    off = 1 if mid else 0
    hold = 2 if m + off >= 6 else 1                              # weeks spent at peak volume
    k_peak = max(1, m + off - hold)                              # build step at which weekly volume first reaches its peak
    level0 = min(cur, cap)
    peak_long = K.long_run_km(cap, goal_km, experience, run_days, long_day_minutes, pace_s)
    long0 = max(recent_longs[-4:], default=0.0)                  # the athlete's recent longest run (mid-plan), the anchor for the long-run ladder

    weeks: list[Week] = []
    level, j, first_long = level0, 0, None
    for i in train:
        recovery = i in recov
        if not recovery:
            step = j + off
            if step > 0:
                ramp_to = level0 + (cap - level0) * min(1.0, step / k_peak)
                level = max(level, min(K.next_week_km(level, experience), ramp_to))
        week_km = level * K.RECOVERY_FACTOR if recovery else level
        frac = i / n_train
        phase = "base" if frac < 0.4 else "build" if frac < 0.8 else "peak"
        ladder = None
        if not recovery:
            top = max(peak_long, long0, first_long or 0.0)
            if long0 > 0:                                        # anchored on a real recent long run: one step toward the peak this week
                ladder = long0 + (top - long0) * min(1.0, (j + 1) / max(1, m))
            elif first_long is not None:                         # fresh plan: the first long run sets the start, then climb evenly
                ladder = first_long + (top - first_long) * min(1.0, j / max(1, m - 1))
        lr = long_run(week_km, ladder=ladder, recovery=recovery)
        if first_long is None and not recovery:
            first_long = lr
        if not recovery:
            j += 1
        weeks.append(Week(start + timedelta(weeks=i), phase, recovery, False, round(week_km * 2) / 2, lr))

    long_shares = K.taper_long_shares(goal_km)
    taper_longs = [x for x in long_shares][-max(0, taper - 1):] if taper > 1 else []
    peak_training_long = max([w.long_run_km for w in weeks] + ([longest_recent_km] if longest_recent_km else []) + [peak_long * 0.0], default=0.0)
    for k, mult in enumerate(mults):
        i = n_train + k
        if i < first:
            continue
        race = i == n - 1
        week_km = level * mult
        if race:  # the race itself is part of race week; plus a modest amount of easy running around it
            week_km = K.race_week_km(goal_km, level)
            long_km = 0.0
        else:
            share = taper_longs[k] if k < len(taper_longs) else 0.5
            long_km = min(long_run(week_km), math.floor(share * max(peak_training_long, 1.0) * 2) / 2)
        weeks.append(Week(start + timedelta(weeks=i), "race" if race else "taper", False, race, round(week_km * 2) / 2, long_km))

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
        weekly_minutes=weekly_minutes, pace_s_km=pace_s, weeks_to_race=wtr, min_weeks_=outlook.min_weeks, ideal_low=low,
        baseline_km=baseline_km if baseline_source == "stated" else 0.0, run_days=run_days)
    return weeks, outlook


def build_skeleton(**kw) -> list[Week]:
    return build_skeleton_info(**kw)[0]
