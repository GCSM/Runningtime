"""Plan quality audit: does a plan have the SHAPE and the SAFETY of a published plan?

Used three ways: by the test-suite across thousands of athletes, by `tools/plan_eval.py` against plans the real model writes, and as the
written definition of "a good plan" (every check below says why it exists). Nothing here changes a plan; it only reports problems.

Shape checks (on the weekly skeleton): volume and long run build gradually and arrive at their peak near the end, the peak long run happens
once or twice (not every week), every fourth week steps back, and the taper steps down. Rule checks (on day-by-day workouts) cover
availability, weekly totals, hard-session spacing and run caps.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import knowledge as K
from .models import DAYS, Profile
from .skeleton import Week, monday

TRAIN = ("base", "build", "peak")


def audit_shape(weeks: list[Week], goal_km: float, level: str) -> list[str]:
    """Problems with the weekly volume / long-run curve of a skeleton. Empty list = a sensibly periodized build."""
    v: list[str] = []
    train = [w for w in weeks if w.phase in TRAIN]
    taper = [w for w in weeks if w.phase == "taper"]
    if len(train) < 3:
        return v
    n = len(train)
    build = [w for w in train if not w.recovery]
    # --- peak long run: built up to, reached late, not repeated every week ---------------------------------------------------
    peak_long = max(w.long_run_km for w in train)
    at_peak = [i for i, w in enumerate(train) if peak_long and w.long_run_km >= 0.99 * peak_long]
    can_build_long = train[0].long_run_km < 0.85 * peak_long        # an athlete who starts at the peak (or is capped by time) has nothing to build
    if n >= 8 and can_build_long:
        if len(at_peak) > 2:
            v.append(f"the peak long run ({peak_long:.1f} km) is repeated in {len(at_peak)} weeks; a plan should peak once or twice")
        if at_peak and at_peak[0] < 0.7 * n:
            v.append(f"the peak long run first appears in week {at_peak[0] + 1} of {n}: reached too early, then held")
    # --- peak weekly volume: same idea ---------------------------------------------------------------------------------------
    peak_km = max(w.target_km for w in train)
    top = [i for i, w in enumerate(train) if not w.recovery and w.target_km >= 0.99 * peak_km]
    can_build_vol = train[0].target_km < 0.85 * peak_km
    if n >= 8 and can_build_vol:
        if len(top) > 3:
            v.append(f"peak weekly volume ({peak_km:.1f} km) is held for {len(top)} weeks")
        if top and top[0] < 0.6 * n:
            v.append(f"peak weekly volume first reached in week {top[0] + 1} of {n}: reached too early")
    # --- build weeks never go backwards; long run grows no faster than the rule ----------------------------------------------
    for a, b in zip(build, build[1:]):
        if b.target_km < a.target_km - 0.6:
            v.append(f"weekly volume drops {a.target_km:.1f} to {b.target_km:.1f} km between build weeks of {a.week_start}")
        if b.long_run_km < a.long_run_km - 0.6 and b.long_run_km > 0:
            v.append(f"long run drops {a.long_run_km:.1f} to {b.long_run_km:.1f} km between build weeks of {a.week_start}")
    for i, w in enumerate(train):
        prior = max((x.long_run_km for x in train[max(0, i - 4):i]), default=0.0)
        if prior and w.long_run_km > max(prior * (1 + K.LONG_RUN_GROWTH_PCT), prior + K.LONG_RUN_GROWTH_FLOOR_KM) + 0.5:
            v.append(f"long run jumps to {w.long_run_km:.1f} km after a recent longest of {prior:.1f} km (week of {w.week_start})")
    # --- step-back weeks -----------------------------------------------------------------------------------------------------
    run = 0
    for i, w in enumerate(train):
        run = 0 if w.recovery else run + 1
        if run > K.RECOVERY_EVERY + 1 and n >= K.MIN_WEEKS_FOR_RECOVERY:
            v.append(f"{run} build weeks in a row without a step-back week (week of {w.week_start})")
        if w.recovery and i:
            prev = max((x for x in train[:i] if not x.recovery), key=lambda x: x.week_start, default=None)
            if prev and (w.target_km > 0.8 * prev.target_km + 0.5 or (prev.long_run_km and w.long_run_km > 0.8 * prev.long_run_km + 0.5)):
                v.append(f"recovery week of {w.week_start} isn't lighter enough than the build week before it")
    # --- the very first weeks -------------------------------------------------------------------------------------------------
    if train[0].long_run_km > 0.45 * train[0].target_km + 0.5 and train[0].target_km > 0:
        v.append(f"the first long run ({train[0].long_run_km:.1f} km) is {train[0].long_run_km / train[0].target_km:.0%} of a {train[0].target_km:.1f} km week")
    # --- taper ----------------------------------------------------------------------------------------------------------------
    last_train = train[-1]
    prev_km, prev_long = last_train.target_km, last_train.long_run_km
    for w in taper + [x for x in weeks if x.phase == "race"]:
        if w.target_km >= prev_km + 0.01 and not w.race_week:
            v.append(f"taper week of {w.week_start} ({w.target_km:.1f} km) isn't lower than the week before ({prev_km:.1f})")
        if w.long_run_km > prev_long + 0.01:
            v.append(f"taper long run of {w.week_start} ({w.long_run_km:.1f} km) is longer than the one before")
        prev_km, prev_long = w.target_km, (w.long_run_km if w.long_run_km else prev_long)
    return v


def audit_plan(workouts: list[dict], profile: Profile, weeks: list[Week], today: date, *, run_cap_check: bool = True, check_volume: bool = True) -> list[str]:
    """Rule violations in a day-by-day plan (model-written or code-written). Independent of the app's sanitizer."""
    v: list[str] = []
    sk = {w.week_start.isoformat(): w for w in weeks}
    by_week: dict[str, list[dict]] = {}
    seen: dict[str, int] = {}
    for w in sorted(workouts, key=lambda w: w["date"]):
        seen[w["date"]] = seen.get(w["date"], 0) + 1
        d = date.fromisoformat(w["date"])
        if d < today or d > profile.race_date:
            v.append(f"{w['date']}: outside the plan window")
        av = profile.availability[DAYS[d.weekday()]]
        if w["type"] != "race" and not av.available:
            v.append(f"{w['date']}: run on an unavailable day")
        if w["type"] not in ("race", "long") and av.available and av.minutes and w["duration_min"] > av.minutes + 1:
            v.append(f"{w['date']}: {w['duration_min']} min is longer than that day's {av.minutes}")
        if run_cap_check and w["type"] not in ("race", "long", "cross") and w["duration_min"] > K.run_cap_minutes(profile.experience, w["type"], profile.goal_km) + 1:
            v.append(f"{w['date']}: {w['type']} run of {w['duration_min']} min is over the weekday cap")
        by_week.setdefault(monday(d).isoformat(), []).append(w)
    v += [f"{d}: {n} workouts on one day" for d, n in seen.items() if n > 1]
    race = [w for w in workouts if w["type"] == "race"]
    if len(race) != 1 or race[0]["date"] != profile.race_date.isoformat():
        v.append("race day workout missing or on the wrong date")
    for ws, items in sorted(by_week.items()):
        s = sk.get(ws)
        if not s:
            continue
        km = sum(w["distance_km"] for w in items)
        if check_volume and s.phase != "race" and s.target_km and not (0.85 <= km / s.target_km <= 1.12) and date.fromisoformat(ws) >= monday(today) + timedelta(days=7):
            v.append(f"week {ws}: {km:.1f} km vs target {s.target_km}")
        q = [w for w in items if w["type"] in K.QUALITY_TYPES]
        if len(q) > K.quality_limit(profile.experience, s.phase if s.phase != "race" else "taper", s.recovery, sum(a.available for a in profile.availability.values())):
            v.append(f"week {ws}: {len(q)} hard sessions is more than allowed")
        if s.recovery and q:
            v.append(f"week {ws}: hard session in a recovery week")
        longs = [w for w in items if w["type"] == "long"]
        for a, b in zip(q, q[1:]):
            if (date.fromisoformat(b["date"]) - date.fromisoformat(a["date"])).days < 2:
                v.append(f"week {ws}: hard sessions on consecutive days")
        for x in q:
            for lg in longs:
                if abs((date.fromisoformat(x["date"]) - date.fromisoformat(lg["date"])).days) == 1:
                    v.append(f"week {ws}: hard session next to the long run")
        for lg in longs:
            if DAYS[date.fromisoformat(lg["date"]).weekday()] != profile.long_run_day:
                v.append(f"week {ws}: long run isn't on {profile.long_run_day}")
    # --- sessions must progress, and hard efforts must not stack ---------------------------------------------------------------
    from .plancheck import is_paced_long
    hard = sorted([w for w in workouts if w["type"] in K.QUALITY_TYPES or is_paced_long(w)], key=lambda w: w["date"])
    for i, w in enumerate(hard):
        d0 = date.fromisoformat(w["date"])
        in_window = [x for x in hard[i:] if (date.fromisoformat(x["date"]) - d0).days <= 6]
        if len(in_window) > 2 and not any(abs((date.fromisoformat(x["date"]) - d0).days) > 6 for x in in_window[:1]):
            v.append(f"{len(in_window)} hard efforts in 7 days starting {w['date']}")
            break
    chain, last = 0, None
    for ws, items in sorted(by_week.items()):
        q = sorted([w for w in items if w["type"] in K.QUALITY_TYPES], key=lambda w: w["date"])
        sig = (q[0]["type"], q[0]["title"].strip().lower(), round(q[0]["distance_km"])) if q else None
        chain = chain + 1 if sig and sig == last else (1 if sig else 0)
        last = sig
        if chain >= 4:
            v.append(f"the same quality session ('{q[0]['title']}') is repeated {chain} weeks running (week {ws})")
            break
    long_by_week = {ws: max(w["distance_km"] for w in items if w["type"] == "long") for ws, items in by_week.items() if any(w["type"] == "long" for w in items)}
    peak = max(long_by_week.values(), default=0)
    if peak > K.long_run_cap_km(profile.goal_km, profile.experience) * 1.06:
        v.append(f"peak long run {peak} km is over the {K.long_run_cap_km(profile.goal_km, profile.experience)} km cap")
    return v


def weeks_from_workouts(workouts: list[dict], skeleton: list[Week]) -> list[Week]:
    """The plan as the athlete will experience it: each week's real total and long run (phases/recovery flags from the skeleton), for audit_shape."""
    by_week: dict[str, list[dict]] = {}
    for w in workouts:
        by_week.setdefault(w["week_start"] if "week_start" in w else monday(date.fromisoformat(w["date"])).isoformat(), []).append(w)
    out = []
    for sk in skeleton:
        items = by_week.get(sk.week_start.isoformat(), [])
        km = round(sum(w["distance_km"] for w in items), 1)
        long_km = max((w["distance_km"] for w in items if w["type"] == "long"), default=0.0)
        out.append(Week(sk.week_start, sk.phase, sk.recovery, sk.race_week, km, long_km))
    return out
