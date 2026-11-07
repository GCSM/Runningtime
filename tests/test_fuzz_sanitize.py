"""Hostile and malformed model answers: whatever comes back, sanitize must not crash, must return safe numbers, and must obey the hard rules.

Found by fuzzing: ~46% of random answers crashed the old sanitizer (NaN/inf, non-dict entries, a division by zero) and ~25% of the survivors
broke a rule (a 29 km run listed as 3 minutes, long runs on the wrong day, hard sessions next to the long run across a Monday, wrong race distance).
"""
import math
import random
from datetime import date, timedelta

from coach import knowledge as K
from coach.models import DAYS, WORKOUT_TYPES, DayAvail, Profile
from coach.plancheck import sanitize
from coach.planaudit import audit_plan
from coach.skeleton import build_skeleton_info

T = date(2026, 11, 9)
GARBAGE_NUMBERS = [None, "12", "abc", "", -5, -0.0, 0, 0.0, 1e9, float("inf"), float("-inf"), float("nan"), 3.14159, 10, 25.5, 42, True, False, [], {}, "10 km", "1e3", 1e-9]


def make_raw(r, dates, race):
    out = []
    for _ in range(r.randint(0, 90)):
        d = r.choice(dates + [race, T - timedelta(days=3), race + timedelta(days=4), "2026-02-30", "garbage", None, 20261201])
        t = r.choice(list(WORKOUT_TYPES) + ["shakeout", "", None, 5, "RACE", "Easy"])
        w = {"date": d.isoformat() if isinstance(d, date) else d, "type": t, "title": r.choice(["Run", "", None, "x" * 500, 5, "Easy 12 km"]),
             "description": r.choice(["", None, "d" * 3000, 7]), "distance_km": r.choice([r.choice(GARBAGE_NUMBERS), r.uniform(1, 40), r.uniform(1, 40)]),
             "duration_min": r.choice([r.choice(GARBAGE_NUMBERS), r.randint(10, 200), r.randint(10, 200)]), "pace_guidance": r.choice(["", None, "5:00/km", 3])}
        for k in r.sample(list(w), r.randint(0, 2)):
            w.pop(k)
        if r.random() < 0.05:
            w["extra"] = {"nested": [1, 2]}
        out.append(w)
    if r.random() < 0.1:
        out += ["not a dict", None, 7, ["list"]]
    return out


def test_sanitize_survives_and_obeys_the_hard_rules_for_thousands_of_hostile_answers():
    R = random.Random(2026)
    crashes, violations = [], []
    for n in range(1500):
        goal = R.choice([5.0, 10.0, 21.0975, 42.195, 15.0])
        level = R.choice(K.LEVELS)
        days = R.sample(DAYS, R.randint(2, 6))
        long_day = R.choice(days)
        race = T + timedelta(weeks=R.randint(3, 26), days=R.randint(0, 6))
        today = T + timedelta(days=R.randint(0, 6))
        p = Profile(name="x", goal_label="g", goal_km=goal, race_date=race, baseline_km=R.choice([0, 10, 30, 60]), experience=level,
                    availability={d: DayAvail(available=d in days, minutes=R.choice([30, 45, 60, 90, 150, 240])) for d in DAYS}, long_run_day=long_day,
                    goal_time_s=R.choice([None, None, int(goal * 330)]))
        weeks, _ = build_skeleton_info(plan_start=today, today=today, race_date=race, goal_km=goal, baseline_km=p.baseline_km, experience=level,
                                       weekly_minutes=p.weekly_minutes, long_day_minutes=p.long_day_minutes, easy_pace_s_km=390.0, run_days=len(days))
        dates = [today + timedelta(days=i) for i in range((race - today).days + 1)]
        raw = make_raw(R, dates, race)
        try:
            res, notes = sanitize(raw, profile=p, weeks=weeks, today=today, blocked_dates=set(), done_km_by_week={}, pace_s_km=390.0, level=level)
        except Exception as e:  # noqa: BLE001
            crashes.append(f"case {n}: {type(e).__name__}: {e}")
            continue
        for x in res:
            for key in ("distance_km", "duration_min"):
                v = x[key]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                    violations.append(f"case {n}: {key}={v!r}")
            if x["type"] not in WORKOUT_TYPES:
                violations.append(f"case {n}: type {x['type']!r}")
            if len(x["title"]) > 120:
                violations.append(f"case {n}: title too long")
            if x["type"] not in ("race", "cross") and x["distance_km"] > 0 and x["duration_min"] > 0:
                pace = x["duration_min"] * 60 / x["distance_km"]
                if x["distance_km"] >= 2 and not (0.45 * 390 <= pace <= 1.9 * 390):
                    violations.append(f"case {n}: implausible pace {pace:.0f}s/km on {x['date']}")
        for pr in audit_plan(list(res), p, weeks, today, check_volume=False):
            violations.append(f"case {n}: {pr}")
    assert not crashes, f"{len(crashes)} crashes, e.g. {crashes[:3]}"
    assert not violations, f"{len(violations)} rule violations, e.g. {violations[:6]}"
