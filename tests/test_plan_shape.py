"""What a good plan LOOKS like, checked across thousands of athletes. Added after a plan gave a 32 km long run every week for months:
the earlier invariants (caps, spacing, totals) all passed while the shape (volume reaching its peak early and then sitting there) was wrong."""
import itertools
from datetime import date, timedelta

import pytest

from coach import knowledge as K
from coach.planaudit import TRAIN, audit_plan, audit_shape
from coach.skeleton import build_skeleton_info

T = date(2026, 11, 9)
GOALS = (5.0, 10.0, 21.0975, 42.195)


def skel(goal, level, days, base, weeks, mins=900, long_min=300, pace=390.0):
    w, o = build_skeleton_info(plan_start=T, today=T, race_date=T + timedelta(weeks=weeks, days=-1), goal_km=goal, baseline_km=base, experience=level,
                               weekly_minutes=mins, long_day_minutes=long_min, easy_pace_s_km=pace, run_days=days)
    return w, o


def grid():
    for goal, level, days, base in itertools.product(GOALS, K.LEVELS, (3, 4, 5, 6), (0, 15, 30, 50)):
        for weeks in (K.min_weeks(goal, level), K.min_weeks(goal, level) + 4, 20, 26, 32):
            if weeks >= 6:
                yield goal, level, days, base, weeks


@pytest.mark.parametrize("mins", [260, 900])
def test_every_generated_skeleton_has_a_sensible_periodized_shape(mins):
    bad = []
    n = 0
    for goal, level, days, base, weeks in grid():
        for lm in (60, 150, 300):
            w, _ = skel(goal, level, days, base, weeks, mins=mins * days // 4, long_min=lm)
            n += 1
            for p in audit_shape(w, goal, level):
                bad.append(f"{K.goal_name(goal)} {level} {days}d base{base} {weeks}wk long{lm}: {p}")
    assert n > 2000
    assert not bad, f"{len(bad)} shape problems, e.g.\n" + "\n".join(bad[:15])


def test_the_reported_plan_marathon_long_run_builds_instead_of_sitting_at_32():
    w, o = skel(42.195, "intermediate", 5, 40, 24)
    longs = [x.long_run_km for x in w if x.phase in TRAIN]
    assert longs[0] <= 17 and max(longs) == 32.0 and sum(1 for k in longs if k >= 31) <= 2        # was 32 km in ELEVEN weeks of the 24
    build = [x.long_run_km for x in w if x.phase in TRAIN and not x.recovery]
    assert all(b >= a - 0.5 for a, b in zip(build, build[1:]))                                    # climbs, never backwards between build weeks
    assert longs.index(32.0) >= int(0.8 * len(longs))                                             # the biggest run comes late
    vols = [x.target_km for x in w if x.phase in TRAIN and not x.recovery]
    assert sum(1 for v in vols if v >= 0.97 * max(vols)) <= 3                                    # was 9 identical weeks at 78 km


def test_a_long_plan_is_not_rushed_and_a_short_plan_is_ramp_limited_not_cheated():
    long_plan, _ = skel(42.195, "intermediate", 5, 40, 32)
    short_plan, o = skel(42.195, "intermediate", 5, 40, 16)
    first_peak_long = next(i for i, x in enumerate([x for x in long_plan if x.phase in TRAIN]) if x.long_run_km >= 31)
    assert first_peak_long >= 20                                                                  # 32 weeks: the 32 km run is nowhere near week 10
    assert max(x.long_run_km for x in short_plan if x.phase in TRAIN) <= 32
    assert audit_shape(short_plan, 42.195, "intermediate") == []                                  # still gradual: it simply peaks lower


def test_shape_audit_catches_the_old_behaviour():
    """The audit itself must fail on the plateau it was written for (otherwise it proves nothing)."""
    from coach.skeleton import Week
    wk = []
    for i in range(20):                                      # the old shape: ramp as fast as allowed, then sit at the peak every week
        rec = i % 4 == 3
        vol, lng = min(78.0, 40 + 9 * i), min(32.0, 16 + 4 * i)
        wk.append(Week(T + timedelta(weeks=i), "build", rec, False, vol * (0.75 if rec else 1), lng * (0.75 if rec else 1)))
    problems = " ".join(audit_shape(wk, 42.195, "intermediate"))
    assert "repeated in" in problems and "reached too early" in problems
    ok, _ = skel(42.195, "intermediate", 5, 40, 20)
    assert audit_shape(ok, 42.195, "intermediate") == []


def test_peak_long_run_and_volume_reach_published_levels_when_there_is_time_and_availability():
    for level, days, base, lo_long in [("intermediate", 5, 35, 31), ("intermediate", 4, 35, 31), ("advanced", 6, 50, 31)]:
        w, o = skel(42.195, level, days, base, 22)
        assert o.peak_long_km >= lo_long and o.peak_km >= K.peak_range(42.195, level)[0] * (0.95 if days >= 5 else 0.85), (level, days, o.peak_km)
    w, _ = skel(21.0975, "intermediate", 5, 30, 14)
    assert max(x.long_run_km for x in w) >= 19 and max(x.target_km for x in w) >= 50


def test_full_plans_from_the_reference_coach_pass_the_rule_audit_across_goals(tmp_path):
    from test_scenarios import make_service, prof, TODAY
    for goal, level, days, weeks, base in [(5.0, "beginner", ("tue", "thu", "sun"), 10, 10), (10.0, "intermediate", ("tue", "wed", "fri", "sun"), 12, 25),
                                           (21.0975, "intermediate", ("mon", "tue", "thu", "sat", "sun"), 14, 30), (42.195, "intermediate", ("mon", "tue", "thu", "sat", "sun"), 22, 40),
                                           (42.195, "intermediate", ("tue", "thu", "sat", "sun"), 20, 35)]:
        p = prof(goal_km=goal, weeks=weeks, level=level, baseline=base, days=days, minutes=75, long_minutes=240)
        sub = tmp_path / f"{goal}-{weeks}-{len(days)}"
        sub.mkdir()
        svc, udb, _ = make_service(sub, p)
        svc.generate_plan()
        inp = svc._skeleton(p, [], TODAY)
        problems = audit_plan(udb.get_workouts(), p, inp.weeks, TODAY)
        assert problems == [], (goal, level, problems[:6])
        assert audit_shape(inp.weeks, goal, level) == []


@pytest.mark.parametrize("goal,level,days,base,weeks", [(42.195, "intermediate", 5, 40, 24), (42.195, "intermediate", 4, 35, 20), (21.0975, "intermediate", 5, 30, 16),
                                                         (10.0, "beginner", 4, 15, 14)])
def test_weekly_auto_reviews_keep_the_shape_the_athlete_actually_experiences(goal, level, days, base, weeks):
    """Plans are rebuilt every Monday from what the athlete really ran. Following the plan exactly, week after week, must still give a gradual
    build with one or two peak long runs, not a plateau that re-appears because each review starts from the current week."""
    from coach.skeleton import Week
    race = T + timedelta(weeks=weeks, days=-1)
    executed: list[Week] = []
    for t in range(weeks - 1):
        today = T + timedelta(weeks=t)
        recent = executed[-4:]
        # what the athlete has done, measured the way the app measures it: baseline from the last four complete weeks of runs
        from coach.adherence import recent_baseline_km
        runs = [{"date": (x.week_start + timedelta(days=1)).isoformat(), "distance_m": x.target_km * 1000} for x in executed]
        done_km = (recent_baseline_km(runs, today) if len(executed) >= 4 else base)
        w, _ = build_skeleton_info(plan_start=T, today=today, race_date=race, goal_km=goal, baseline_km=done_km, experience=level, weekly_minutes=900,
                                   long_day_minutes=300, easy_pace_s_km=390.0, run_days=days, longest_recent_km=max((x.long_run_km for x in recent), default=0.0))
        executed.append(w[0])
    problems = audit_shape(executed, goal, level)
    assert problems == [], problems
    peaks = [x.long_run_km for x in executed if x.phase in TRAIN]
    assert sum(1 for k in peaks if k >= 0.99 * max(peaks)) <= 2
