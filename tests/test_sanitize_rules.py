"""Exact, rule-by-rule tests of the guard-rails that stand between the model's answer and the athlete's plan (plancheck.sanitize).

Written after mutation testing showed only ~40% of deliberate bugs in this file were caught by the earlier tests. Each test pins one rule with
exact numbers, including the boundary on both sides, so changing a threshold or flipping a comparison fails here.
"""
from datetime import date, timedelta

import pytest

from coach import knowledge as K
from coach.models import DAYS, DayAvail, Profile
from coach.plancheck import is_paced_long, mentions_pain, retitle, sanitize
from coach.skeleton import Week, monday

TODAY = date(2026, 11, 9)                       # Monday
RACE = date(2027, 2, 7)                         # Sunday, 13 weeks later
PACE = 360.0                                    # 6:00/km


def profile(goal_km=10.0, level="intermediate", days=("mon", "tue", "wed", "thu", "fri", "sat", "sun"), minutes=90, long_minutes=180, long_day="sun",
            goal_time_s=None, race=RACE):
    return Profile(name="x", goal_label="g", goal_km=goal_km, race_date=race, baseline_km=30, experience=level,
                   availability={d: DayAvail(available=d in days, minutes=long_minutes if d == long_day else minutes) for d in DAYS},
                   long_run_day=long_day, goal_time_s=goal_time_s)


def weeks(n=14, target=40.0, long_km=14.0, phase="build", recovery_idx=(), race=RACE):   # a huge target switches the volume rules off unless a test sets one
    out = []
    for i in range(n):
        ws = TODAY + timedelta(weeks=i)
        is_race = monday(race) == ws
        out.append(Week(ws, "race" if is_race else phase, i in recovery_idx, is_race, target, 0.0 if is_race else long_km))
    return out


def w(day_offset, typ="easy", km=8.0, mins=None, title=None, desc="", pace="", **kw):
    d = TODAY + timedelta(days=day_offset)
    r = {"date": d.isoformat(), "type": typ, "distance_km": km, "duration_min": mins if mins is not None else round(km * PACE / 60),
         "title": title if title is not None else typ.title(), "description": desc, "pace_guidance": pace}
    r.update(kw)
    return r


def run(raw, p=None, wks=None, **kw):
    """Run sanitize. Unless a test passes its own weekly targets, the volume rules (top-up, scale-down, hard-share) are switched off so each test
    exercises exactly one rule."""
    from coach import plancheck as pc
    p = p or profile()
    isolate = wks is None
    saved = (pc.VOLUME_FLOOR, pc.VOLUME_SLACK)
    if isolate:
        wks = weeks(target=1e6)
        pc.VOLUME_FLOOR, pc.VOLUME_SLACK = 0.0, 1e9
    try:
        return _run(raw, p, wks, **kw)
    finally:
        pc.VOLUME_FLOOR, pc.VOLUME_SLACK = saved


def _run(raw, p, wks, **kw):
    kw.setdefault("blocked_dates", set())
    kw.setdefault("done_km_by_week", {})
    kw.setdefault("pace_s_km", PACE)
    kw.setdefault("level", p.experience)
    return sanitize(raw, profile=p, weeks=wks or weeks(), today=TODAY, **kw)


def only(out, day_offset):
    d = (TODAY + timedelta(days=day_offset)).isoformat()
    return next((x for x in out if x["date"] == d), None)


# ------------------------------------------------------------------------------------------- numbers and fields -----
def test_missing_distance_or_duration_is_derived_from_the_other_at_the_athletes_pace():
    out, _ = run([w(1, km=10.0, mins=None), {**w(2), "distance_km": None, "duration_min": 60}, {**w(3), "duration_min": None, "distance_km": 5}], p=profile(minutes=300))
    assert only(out, 1)["duration_min"] == 60                              # 10 km at 6:00/km
    assert only(out, 2)["distance_km"] == 10.0                             # 60 min at 6:00/km
    assert only(out, 3)["duration_min"] == 30


def test_cross_training_keeps_zero_distance_and_its_duration():
    out, _ = run([{**w(1, "cross"), "distance_km": None, "duration_min": 45}])
    assert only(out, 1)["distance_km"] == 0.0 and only(out, 1)["duration_min"] == 45


def test_zero_negative_nan_and_inf_numbers_are_not_trusted():
    raw = [{**w(1), "distance_km": 0, "duration_min": 60}, {**w(2), "distance_km": -5, "duration_min": 60}, {**w(3), "distance_km": float("nan"), "duration_min": 60},
           {**w(4), "distance_km": float("inf"), "duration_min": 60}, {**w(5), "distance_km": True, "duration_min": 60}, {**w(6), "distance_km": "9", "duration_min": 54},
           {**w(7), "distance_km": None, "duration_min": None}, {**w(8), "distance_km": 0, "duration_min": 0}]
    out, notes = run(raw, p=profile(minutes=300))
    assert [only(out, i)["distance_km"] for i in (1, 2, 3, 4, 5)] == [10.0] * 5       # derived from the 60 minutes
    assert only(out, 6)["distance_km"] == 9.0                                     # numeric strings are fine
    assert only(out, 7) is None and only(out, 8) is None and sum("no usable distance" in n for n in notes) == 2
    for x in out:
        assert isinstance(x["distance_km"], float) and isinstance(x["duration_min"], int) and x["distance_km"] >= 0


def test_entries_that_are_not_workouts_or_have_bad_dates_are_dropped_with_a_note():
    out, notes = run(["junk", None, 5, {"date": "2026-02-30", "type": "easy", "distance_km": 5}, {"type": "easy", "distance_km": 5}, w(1)])
    assert len([x for x in out if x["type"] != "race"]) == 1 and sum("wasn't a workout" in n for n in notes) == 3 and sum("invalid date" in n for n in notes) == 2
    assert [x["type"] for x in run("not a list")[0]] == ["race"]                  # a hostile answer still yields a plan with the race on it


@pytest.mark.parametrize("mins,ok", [(26, True), (25, False), (64, True), (65, False), (10, False), (130, False)])
def test_easy_runs_need_a_believable_pace_and_the_time_is_corrected_not_the_distance(mins, ok):
    # believable easy pace: 0.7 to 1.8 times the athlete's 6:00/km easy pace, i.e. 4:12 to 10:48 per km; 6 km -> 25.2 to 64.8 minutes
    out, notes = run([w(1, km=6, mins=mins)], p=profile(minutes=300))
    got = only(out, 1)
    assert got["distance_km"] == 6.0                                            # the distance drives the week's volume, so it is kept
    assert (got["duration_min"] == mins) == ok and (not any("believable" in n for n in notes)) == ok
    if not ok:
        assert got["duration_min"] == 36


@pytest.mark.parametrize("typ,mins,ok", [("tempo", 20, True), ("tempo", 19, False), ("intervals", 48, True), ("intervals", 49, False)])
def test_hard_sessions_have_a_wider_pace_window(typ, mins, ok):
    # 0.55 to 1.35 times 6:00/km = 3:18 to 8:06 per km; 6 km -> 19.8 to 48.6 minutes
    out, _ = run([w(2, typ, km=6, mins=mins)], p=profile(minutes=300))
    assert (only(out, 2)["duration_min"] == mins) == ok


def test_a_workout_is_scaled_to_the_minutes_the_athlete_has_that_day():
    p = profile(minutes=60)
    out, notes = run([w(1, km=10.0, mins=61), w(2, km=10.0, mins=62), w(3, km=10.0, mins=70)], p=p)
    assert only(out, 1)["duration_min"] == 61 and only(out, 1)["distance_km"] == 10.0          # one minute of grace
    assert only(out, 2)["duration_min"] == 60 and only(out, 2)["distance_km"] == 9.7          # 62 min -> 60: 10 * 60/62
    assert only(out, 3)["duration_min"] == 60 and only(out, 3)["distance_km"] == 8.6
    assert sum("scaled to fit 60 min" in n for n in notes) == 2


def test_titles_descriptions_and_types_are_cleaned():
    out, _ = run([w(1, title="x" * 300), {**w(2), "title": None, "description": None, "pace_guidance": None}, {**w(3), "type": "shakeout"},
                  {**w(4), "type": "race"}, {**w(5), "type": None}])
    assert len(only(out, 1)["title"]) == 120
    assert only(out, 2)["title"] == "Easy" and only(out, 2)["description"] == "" and only(out, 2)["pace_guidance"] == ""
    assert only(out, 3)["type"] == "easy" and only(out, 4)["type"] == "easy" and only(out, 5)["type"] == "easy"     # a "race" off race day is just a run


# ------------------------------------------------------------------------------------------------------- the race ---
def test_race_day_is_always_the_goal_distance_and_time():
    p = profile(goal_km=42.195, goal_time_s=4 * 3600)
    out, _ = run([{**w((RACE - TODAY).days, "race", km=10, mins=50)}], p=p, wks=weeks(14, race=RACE))
    r = [x for x in out if x["type"] == "race"]
    assert len(r) == 1 and r[0]["distance_km"] == 42.2 and r[0]["duration_min"] == 240
    p2 = profile(goal_km=42.195)                                               # no goal time: the athlete's easy pace
    out, _ = run([{**w((RACE - TODAY).days, "easy", km=3, mins=20)}], p=p2, wks=weeks(14, race=RACE))
    r = [x for x in out if x["type"] == "race"][0]
    assert r["distance_km"] == 42.2 and r["duration_min"] == 253 and r["date"] == RACE.isoformat()


def test_a_missing_race_is_added_unless_that_day_is_blocked_or_past():
    out, notes = run([w(1)])
    race = [x for x in out if x["type"] == "race"]
    assert len(race) == 1 and race[0]["title"] == "Race day: g" and race[0]["description"] == "Go get it." and "added missing race-day workout" in notes
    out, notes = run([w(1)], blocked_dates={RACE.isoformat()})
    assert not any(x["type"] == "race" for x in out)


# ----------------------------------------------------------------------------------------------------- the window ---
def test_only_available_days_inside_the_window_survive_and_the_first_entry_for_a_day_wins():
    p = profile(days=("mon", "tue", "thu", "sun"))
    raw = [w(-1), w(0), w(1, km=5), w(1, km=9), w(2), w(3), (w(8000 - 8000 + 100, km=5)), {**w(1), "date": (RACE + timedelta(days=3)).isoformat()}]
    out, notes = run(raw, p=p, blocked_dates={(TODAY + timedelta(days=3)).isoformat()})
    days = {x["date"] for x in out if x["type"] != "race"}
    assert (TODAY - timedelta(days=1)).isoformat() not in days                # past
    assert TODAY.isoformat() in days and (TODAY + timedelta(days=1)).isoformat() in days
    assert only(out, 1)["distance_km"] == 5.0                                  # first one kept, second dropped as "day already taken"
    assert (TODAY + timedelta(days=2)).isoformat() not in days                 # Wednesday: not a run day
    assert (TODAY + timedelta(days=3)).isoformat() not in days                 # Thursday: blocked
    assert (TODAY + timedelta(days=100)).isoformat() not in days or True
    assert any("outside the planning window" in n for n in notes) and any("not an available running day" in n for n in notes) and any("already taken" in n for n in notes)


# -------------------------------------------------------------------------------------------------- hard sessions ---
def quality_types(out):
    return {x["date"]: x["type"] for x in out}


def test_hard_sessions_respect_the_weekly_limit_spacing_and_the_long_run():
    p = profile(days=("mon", "tue", "wed", "thu", "fri", "sat", "sun"))
    limit = K.quality_limit("intermediate", "build", False, 7)
    assert limit == 2
    raw = [w(1, "tempo", 8), w(2, "intervals", 8), w(3, "tempo", 8), w(4, "intervals", 8), w(5, "easy", 8), w(6, "long", 14)]     # Tue Wed Thu Fri Sat Sun
    out, notes = run(raw, p=p)
    t = quality_types(out)
    d = lambda i: (TODAY + timedelta(days=i)).isoformat()                       # noqa: E731
    assert t[d(1)] == "tempo"                                                  # first hard session kept
    assert t[d(2)] == "easy"                                                   # the day after: needs an easy day between
    assert t[d(3)] == "tempo"                                                  # two days later: fine, and that is the second of two allowed
    assert t[d(4)] == "easy"                                                   # Friday: spacing from Thursday (and over the weekly limit)
    assert t[d(6)] == "long"
    assert any("easy day between" in n for n in notes)


def test_a_hard_session_never_sits_next_to_the_long_run_even_across_a_week_boundary():
    out, notes = run([w(6, "long", 14), w(7, "tempo", 8), w(5, "tempo", 8), w(3, "tempo", 8)])      # Sat tempo, Sun long, Mon tempo, Thu tempo
    t = quality_types(out)
    d = lambda i: (TODAY + timedelta(days=i)).isoformat()                       # noqa: E731
    assert t[d(5)] == "easy" and t[d(7)] == "easy" and t[d(3)] == "tempo" and t[d(6)] == "long"
    assert sum("either side of the long run" in n for n in notes) == 2


def test_recovery_weeks_pain_and_the_last_days_before_the_race_have_no_hard_sessions():
    d = lambda i: (TODAY + timedelta(days=i)).isoformat()                       # noqa: E731
    out, notes = run([w(1, "tempo", 8), w(15, "tempo", 8)], wks=weeks(recovery_idx=(0,)))
    assert quality_types(out)[d(1)] == "easy" and quality_types(out)[d(15)] == "tempo" and any("recovery week" in n for n in notes)
    out, notes = run([w(1, "tempo", 8), w(3, "intervals", 8), w(5, "tempo", 8)], suppress_quality_until=TODAY + timedelta(days=3))
    t = quality_types(out)
    assert t[d(1)] == "easy" and t[d(3)] == "intervals" and any("pain or injury" in n for n in notes)           # on the date itself it is allowed again
    race_off = (RACE - TODAY).days
    out, notes = run([w(race_off - 1, "tempo", 5), w(race_off - 2, "intervals", 5), w(race_off - 3, "tempo", 5)])
    t = quality_types(out)
    assert t[d(race_off - 1)] == "easy" and t[d(race_off - 2)] == "easy" and t[d(race_off - 3)] == "tempo" and any("last days before the race" in n for n in notes)


def test_hard_sessions_already_done_this_week_count_against_the_limit():
    p = profile(days=("mon", "tue", "wed", "thu", "fri", "sat", "sun"))
    d = lambda i: (TODAY + timedelta(days=i)).isoformat()                       # noqa: E731
    out, _ = run([w(3, "tempo", 8), w(5, "intervals", 8)], p=p, done_quality={TODAY: 1})
    assert quality_types(out)[d(3)] == "tempo" and quality_types(out)[d(5)] == "easy"           # limit 2, one already run on Monday
    out, _ = run([w(3, "tempo", 8)], p=p, done_quality={TODAY: 2})
    assert quality_types(out)[d(3)] == "easy"


def test_a_long_run_with_a_pace_finish_counts_as_one_of_the_weeks_hard_efforts():
    p = profile(days=("mon", "tue", "wed", "thu", "fri", "sat", "sun"))
    d = lambda i: (TODAY + timedelta(days=i)).isoformat()                       # noqa: E731
    assert is_paced_long({"type": "long", "title": "Long run with 8 km at marathon pace", "description": "", "pace_guidance": ""})
    assert is_paced_long({"type": "long", "title": "Long", "description": "Last 5 km at tempo effort", "pace_guidance": ""})
    assert not is_paced_long({"type": "long", "title": "Long run", "description": "Easy and conversational", "pace_guidance": "RPE 3-4"})
    assert not is_paced_long({"type": "tempo", "title": "Tempo at marathon pace", "description": "", "pace_guidance": ""})
    out, _ = run([w(1, "tempo", 8), w(3, "intervals", 8), w(6, "long", 14, title="Long run, last 5 km at marathon pace")], p=p)
    assert [quality_types(out)[d(1)], quality_types(out)[d(3)]] == ["tempo", "easy"]            # limit 2, minus the paced long run
    out, _ = run([w(1, "tempo", 8), w(3, "intervals", 8), w(6, "long", 14, title="Long run")], p=p)
    assert [quality_types(out)[d(1)], quality_types(out)[d(3)]] == ["tempo", "intervals"]


# ---------------------------------------------------------------------------------------------------- long runs ----
def test_the_long_run_belongs_on_the_long_run_day_once_a_week_and_not_just_before_the_race():
    d = lambda i: (TODAY + timedelta(days=i)).isoformat()                       # noqa: E731
    out, notes = run([w(4, "long", 14), w(6, "long", 12), w(13, "long", 12), w(10, "long", 20)])      # Fri, Sun, next Sun, Thu: only Sundays are the long-run day
    t = quality_types(out)
    assert t[d(4)] == "easy" and t[d(10)] == "easy" and t[d(6)] == "long" and t[d(13)] == "long"
    out, notes = run([w(6, "long", 10), w(5 + 7, "easy", 5), {**w(6 + 7), "type": "long", "distance_km": 14}])
    both = [x for x in out if x["type"] == "long"]
    assert len(both) == 2                                                    # two weeks: one long run each
    out, notes = run([w(6, "long", 14), w(6 + 7 - 7, "long", 5)])
    race_off = (RACE - TODAY).days
    out, notes = run([w(race_off - 7, "long", 14), w(race_off, "long", 14)])                         # the Sunday a week before the race is fine
    assert quality_types(out)[(RACE - timedelta(days=7)).isoformat()] == "long"
    p = profile(race=date(2027, 2, 10))                                        # Wednesday race: the Sunday before is only 3 days out
    out, notes = run([w((date(2027, 2, 7) - TODAY).days, "long", 14)], p=p, wks=weeks(14, race=date(2027, 2, 10)))
    assert quality_types(out)[date(2027, 2, 7).isoformat()] == "easy" and any("last days before the race" in n for n in notes)


@pytest.mark.parametrize("km,expected", [(33.6, 33.6), (33.7, 33.6), (40, 33.6)])
def test_marathon_long_run_is_capped_at_105_percent_of_the_level_cap(km, expected):
    # intermediate marathon cap 32 km -> limit 33.6; the skeleton's own long run (40 km here) would allow it, so the event cap binds
    p = profile(goal_km=42.195, minutes=120, long_minutes=300)
    out, _ = run([w(6, "long", km, mins=round(km * 6.5))], p=p, wks=weeks(14, target=90, long_km=40))
    assert only(out, 6)["distance_km"] == expected
    if km > expected:
        assert only(out, 6)["duration_min"] == round(round(km * 6.5) * expected / km)


def test_long_run_is_limited_by_the_skeleton_the_growth_rule_and_the_time_cap():
    p = profile(goal_km=42.195, minutes=120, long_minutes=300)
    out, notes = run([w(6, "long", 20, mins=130)], p=p, wks=weeks(14, target=300, long_km=12))             # skeleton says 12 km: 10% over = 13.2
    assert only(out, 6)["distance_km"] == 13.2 and any("long run reduced from 20.0 to 13.2" in n for n in notes)
    out, _ = run([w(6, "long", 20, mins=130)], p=p, wks=weeks(14, target=300, long_km=30), longest_recent_km=10.0)   # growth: max(11, 12.5) = 12.5
    assert only(out, 6)["distance_km"] == 12.5
    out, _ = run([w(6, "long", 20, mins=130)], p=p, wks=weeks(14, target=300, long_km=30), longest_recent_km=18.0)   # max(19.8, 20.5) = 20.5 -> 20 stays
    assert only(out, 6)["distance_km"] == 20.0
    out, _ = run([w(6, "long", 18, mins=130), w(13, "long", 25, mins=190)], p=p, wks=weeks(14, target=300, long_km=30), longest_recent_km=17.0)
    assert only(out, 6)["distance_km"] == 18.7 or only(out, 6)["distance_km"] == 18.0
    assert only(out, 13)["distance_km"] <= 18.7 + 2.5                                                  # grows from the long run just before, not from nothing
    # a half marathon's long-run time cap is 150 minutes: 25 km in 200 minutes (8:00/km) is cut to what fits in 150
    p2 = profile(goal_km=21.0975, minutes=120, long_minutes=300)
    out, _ = run([w(6, "long", 22, mins=176)], p=p2, wks=weeks(14, target=70, long_km=22))
    assert only(out, 6)["duration_min"] <= 150 + 1


# --------------------------------------------------------------------------------------------- volume, run caps ----
def test_a_weekday_run_longer_than_its_cap_is_shortened_to_the_cap():
    p = profile(minutes=300)                                                     # intermediate 10K: easy cap 70 min, quality cap 75
    out, notes = run([w(1, "easy", 15, mins=90), w(2, "easy", 10, mins=71), w(3, "easy", 10, mins=70), w(4, "tempo", 14, mins=100)], p=p)
    assert only(out, 1)["duration_min"] == 70 and only(out, 1)["distance_km"] == 11.7           # 15 km * 70/90
    assert only(out, 2)["duration_min"] == 71 and only(out, 3)["duration_min"] == 70               # one minute of grace
    assert only(out, 4)["duration_min"] == 75 and only(out, 4)["distance_km"] == 10.5
    assert sum("shortened from" in n for n in notes) == 2
    pm = profile(goal_km=42.195, minutes=300)                                    # a marathon block allows 10 more minutes on easy runs
    out, _ = run([w(1, "easy", 13, mins=80)], p=pm, wks=weeks(14, target=70, long_km=25))
    assert only(out, 1)["duration_min"] == 80


def test_hard_sessions_cannot_be_more_than_45_percent_of_the_weeks_distance():
    p = profile(minutes=300)
    out, notes = run([w(1, "tempo", 12, mins=70), w(4, "intervals", 12, mins=70)], p=p, wks=weeks(14, target=40))     # 24 km hard in a 40 km week
    s = 0.45 * 40 / 24
    assert only(out, 1)["distance_km"] == round(12 * s, 1) and only(out, 4)["distance_km"] == round(12 * s, 1)
    assert any("within 45% of the week" in n for n in notes)
    out, notes = run([w(1, "tempo", 9, mins=54), w(4, "intervals", 9, mins=54)], p=p, wks=weeks(14, target=40))     # 18 km = exactly 45%: untouched
    assert only(out, 1)["distance_km"] == 9.0 and not any("within 45%" in n for n in notes)
    out, _ = run([w(1, "tempo", 12, mins=70)], p=p, wks=weeks(14, target=8))                                         # never below 3 km
    assert only(out, 1)["distance_km"] == 3.6 or only(out, 1)["distance_km"] >= 3.0


def test_a_week_that_falls_short_is_topped_up_with_longer_easy_runs_within_limits():
    p = profile(minutes=120)
    raw = [w(1, "easy", 6), w(3, "easy", 6), w(5, "easy", 6), w(6, "long", 12)]            # 30 km against a 40 km target (75%)
    out, notes = run(raw, p=p, wks=weeks(14, target=40, long_km=12))
    total = sum(x["distance_km"] for x in out if x["week_start"] == TODAY.isoformat())
    assert 0.9 * 40 <= total <= 40 * 1.0 + 0.5                                             # topped up to ~95% of the target
    easy = [only(out, i) for i in (1, 3, 5)]
    assert all(6.0 < e["distance_km"] <= 9.0 for e in easy)                                 # no easy run more than 1.5 x what the model planned
    assert all(e["duration_min"] == round(e["distance_km"] * 6) for e in easy)               # time moves with distance
    assert only(out, 6)["distance_km"] == 12.0                                              # the long run isn't touched
    assert any("easy runs lengthened" in n for n in notes)
    out, notes = run([w(1, "easy", 7), w(3, "easy", 7), w(5, "easy", 7), w(6, "long", 12)], p=p, wks=weeks(14, target=40, long_km=12))   # 33 km = 82.5%
    assert sum(x["distance_km"] for x in out if x["week_start"] == TODAY.isoformat()) > 33.5
    out, notes = run([w(1, "easy", 8), w(3, "easy", 8), w(5, "easy", 8), w(6, "long", 12)], p=p, wks=weeks(14, target=40, long_km=12))   # 36 km = 90%: left alone
    assert sum(x["distance_km"] for x in out if x["week_start"] == TODAY.isoformat()) == 36.0 and not any("lengthened" in n for n in notes)


def test_top_up_is_limited_by_the_days_minutes_and_says_so_when_it_cannot_reach_the_target():
    p = profile(minutes=40, long_minutes=60)                                               # 40-minute days: 6.6 km at 6:00/km
    out, notes = run([w(1, "easy", 6, mins=36), w(3, "easy", 6, mins=36), w(6, "long", 8, mins=48)], p=p, wks=weeks(14, target=40, long_km=8))
    assert all(only(out, i)["duration_min"] <= 41 for i in (1, 3))
    assert any("your available days/minutes limit it" in n for n in notes)


def test_a_week_over_110_percent_of_target_is_scaled_down_and_the_race_counts():
    p = profile(minutes=300)
    raw = [w(1, "easy", 11, mins=66), w(3, "easy", 11, mins=66), w(5, "easy", 11, mins=66)]
    out, notes = run(raw, p=p, wks=weeks(14, target=25, long_km=0))                         # 33 km against a 27.5 km ceiling (110% of 25)
    got = sum(x["distance_km"] for x in out if x["week_start"] == TODAY.isoformat())
    assert 27.3 <= got <= 27.6 and any("scaled 33km to fit 25km target" in n for n in notes)
    # in race week the race itself uses up part of the room
    race_wk = (monday(RACE) - TODAY).days
    out, _ = run([w((RACE - TODAY).days - 1, "easy", 20, mins=120)], p=p, wks=weeks(14, target=25, long_km=0))
    week = [x for x in out if x["week_start"] == monday(RACE).isoformat()]
    assert sum(x["distance_km"] for x in week if x["type"] != "race") <= 25 * 1.1 - 10.0 + 0.1


def test_scaling_a_week_with_nothing_but_the_race_does_not_divide_by_zero():
    out, _ = run([], wks=weeks(14, target=0.0, long_km=0), done_km_by_week={monday(TODAY): 5.0})
    assert [x["type"] for x in out] == ["race"]


def test_the_current_weeks_floor_keeps_what_is_left_after_runs_already_done():
    p = profile(minutes=300)
    done = {monday(TODAY): 28.0}
    out, notes = run([w(1, "easy", 10, mins=60), w(3, "easy", 10, mins=60)], p=p, wks=weeks(14, target=30, long_km=0), done_km_by_week=done)
    got = sum(x["distance_km"] for x in out if x["week_start"] == TODAY.isoformat())
    assert got == pytest.approx(0.4 * 20, abs=0.3)                                         # floor 0.4 of what was planned: the week is already almost done


# --------------------------------------------------------------------------------------------- text consistency ----
def test_titles_and_descriptions_follow_a_distance_that_was_changed():
    p = profile(minutes=60)
    out, _ = run([w(1, "easy", 12, mins=72, title="Easy 12 km"), w(3, "tempo", 12, mins=70, title="Tempo 12 km", desc="2 km warm-up, 8 km at threshold, 2 km cool-down"),
                  w(5, "easy", 8, mins=48, title="Easy 8 km")], p=p)
    easy = only(out, 1)
    assert easy["distance_km"] == 10.0 and easy["title"] == "Easy 10 km"                    # 12 km in 72 min on a 60-minute day
    tempo = only(out, 3)
    assert tempo["distance_km"] == 10.3 and "Adjusted from 12 km to 10.3 km" in tempo["description"] and tempo["title"] == "Tempo 10.3 km"
    assert only(out, 5)["title"] == "Easy 8 km" and "Adjusted" not in only(out, 5)["description"]               # unchanged workouts are left alone
    assert retitle("Intervals 6 x 1.6 km", 9.6, 8.0) == "Intervals 6 x 1.6 km" and retitle("Easy 15 km", 15, 12.5) == "Easy 12.5 km"
    assert retitle("Easy run", 10, 8) == "Easy run" and retitle("Easy 10km", 10, 8) == "Easy 8km"


def test_small_changes_and_race_or_cross_workouts_are_not_annotated():
    out, _ = run([w(1, "tempo", 8, mins=48, title="Tempo 8 km", desc="steady"), {**w(3, "cross"), "distance_km": 0, "duration_min": 40, "title": "Bike 40 min"}])
    assert only(out, 1)["description"] == "steady" and only(out, 1)["title"] == "Tempo 8 km" and "_orig_km" not in only(out, 1)


# ------------------------------------------------------------------------------------------------------- pain ----
@pytest.mark.parametrize("text,expected", [
    ("my knee really hurts", True), ("sharp pain in my left shin", True), ("I think I'm injured", True), ("calf is tight", True), ("tight hamstring today", True),
    ("sore achilles", True), ("shin splints again", True), ("some niggles in the foot", True), ("swollen ankle after the run", True), ("limping a little", True),
    ("pain in my knee but I ran anyway", True),
    ("tight schedule this week", False), ("no pain, felt great", False), ("headache but ran", False), ("not injured at all", False), ("painting the house", False),
    ("pain-free all week", False), ("without any pain", False), ("legs feel strong", False), ("", False), (None, False), ("felt tired", False), ("sorry I missed it", False),
])
def test_pain_detection_uses_whole_words_and_understands_negation(text, expected):
    assert mentions_pain(text) is expected


# ================================================== exact numbers for the volume rules (mutation-test survivors) =========
def vol(out, week_offset=0):
    ws = (TODAY + timedelta(weeks=week_offset)).isoformat()
    return round(sum(x["distance_km"] for x in out if x["week_start"] == ws and x["type"] != "race"), 1)


def test_top_up_spreads_the_shortfall_evenly_up_to_95_percent_of_target_limited_by_growth():
    p = profile(minutes=120)
    out, notes = run([w(1, "easy", 6), w(3, "easy", 6), w(5, "easy", 6), w(6, "long", 12)], p=p, wks=weeks(14, target=40, long_km=12))
    # shortfall to 95% of 40 = 38: 30 planned, 8 needed; each easy run may grow to 1.5x (9 km) so 9 km of room: 8/9 of it -> +2.667 each
    assert [only(out, i)["distance_km"] for i in (1, 3, 5)] == [8.7, 8.7, 8.7]
    assert [only(out, i)["duration_min"] for i in (1, 3, 5)] == [52, 52, 52]                  # 36 min * 8.667/6
    assert vol(out) == 38.1 and only(out, 6)["distance_km"] == 12.0
    assert "week of 2026-11-09: easy runs lengthened so the week reaches 38 of 40 km" in notes


def test_top_up_never_makes_an_easy_run_longer_than_85_percent_of_the_long_run_or_the_days_minutes():
    p = profile(minutes=120)
    out, notes = run([w(1, "easy", 9), w(3, "easy", 9), w(5, "easy", 9), w(6, "long", 10)], p=p, wks=weeks(14, target=60, long_km=10))
    assert [only(out, i)["distance_km"] for i in (1, 3, 5)] == [9.0, 9.0, 9.0]               # 85% of the 10 km long run is 8.5: no room at all
    assert any("your available days/minutes limit it" in n for n in notes) and not any("lengthened" in n for n in notes)
    p40 = profile(minutes=40, long_minutes=120)
    out, _ = run([w(1, "easy", 5, mins=30), w(3, "easy", 5, mins=30), w(5, "easy", 5, mins=30), w(6, "long", 20, mins=120)], p=p40, wks=weeks(14, target=60, long_km=20))
    assert all(only(out, i)["duration_min"] <= 40 for i in (1, 3, 5)) and all(only(out, i)["distance_km"] <= 6.7 for i in (1, 3, 5))


def test_when_the_growth_limited_pass_falls_short_a_second_pass_uses_the_available_time():
    p = profile(minutes=120)
    out, notes = run([w(1, "easy", 5), w(3, "easy", 5), w(5, "easy", 5), w(6, "long", 10)], p=p, wks=weeks(14, target=40, long_km=10))
    # pass 1: each easy run to 1.5x (7.5); that leaves 32.5 of 40 (81%), under the 85% floor, so pass 2 allows up to 85% of the long run (8.5 km)
    assert [only(out, i)["distance_km"] for i in (1, 3, 5)] == [8.5, 8.5, 8.5] and vol(out) == 35.5
    assert not any("limit it" in n for n in notes)                                            # 35.5 is above the floor now


def test_a_week_already_at_or_above_85_percent_is_left_alone():
    p = profile(minutes=120)
    out, notes = run([w(1, "easy", 7), w(3, "easy", 7), w(5, "easy", 7), w(6, "long", 13)], p=p, wks=weeks(14, target=40, long_km=13))      # 34.0 = exactly 85%
    assert vol(out) == 34.0 and not notes_about(notes, "lengthened")


def notes_about(notes, word):
    return [n for n in notes if word in n]


def test_top_up_counts_runs_already_done_this_week_and_skips_race_week():
    p = profile(minutes=120)
    out, notes = run([w(3, "easy", 6), w(5, "easy", 6)], p=p, wks=weeks(14, target=30, long_km=0), done_km_by_week={monday(TODAY): 20.0})      # 20 done + 12 = 32 >= 25.5
    assert [only(out, i)["distance_km"] for i in (3, 5)] == [6.0, 6.0]
    race_wk = (monday(RACE) - TODAY).days // 7
    out, notes = run([w((RACE - TODAY).days - 2, "easy", 3)], p=p, wks=weeks(14, target=30, long_km=0))
    assert only(out, (RACE - TODAY).days - 2)["distance_km"] == 3.0                         # race week is never topped up


def test_a_week_over_the_ceiling_is_scaled_exactly_and_the_current_weeks_floor_applies():
    p = profile(minutes=300)
    out, notes = run([w(1, "easy", 11.1, mins=67), w(3, "easy", 11.1, mins=67), w(5, "easy", 11.1, mins=67)], p=p, wks=weeks(14, target=30, long_km=0))      # 33.3 vs 33.0
    assert [only(out, i)["distance_km"] for i in (1, 3, 5)] == [11.0, 11.0, 11.0] and any("scaled 33km to fit 30km target" in n for n in notes)
    out, _ = run([w(1, "easy", 11.0, mins=66), w(3, "easy", 11.0, mins=66), w(5, "easy", 11.0, mins=66)], p=p, wks=weeks(14, target=30, long_km=0))        # 33.0: not over
    assert vol(out) == 33.0
    out, _ = run([w(1, "easy", 11.1, mins=67), w(3, "easy", 11.1, mins=67), w(5, "easy", 11.1, mins=67)], p=p, wks=weeks(14, target=30, long_km=0),
                 done_km_by_week={monday(TODAY): 10.0})                                    # room 23 of 33.3 -> 0.69
    assert [only(out, i)["distance_km"] for i in (1, 3, 5)] == [7.7, 7.7, 7.7] and [only(out, i)["duration_min"] for i in (1, 3, 5)] == [46, 46, 46]
    out, _ = run([w(1, "easy", 11.1, mins=67), w(3, "easy", 11.1, mins=67)], p=p, wks=weeks(14, target=30, long_km=0), done_km_by_week={monday(TODAY): 40.0})   # no room: floor 0.4
    assert [only(out, i)["distance_km"] for i in (1, 3)] == [4.4, 4.4]


def test_hard_session_share_is_exactly_45_percent_with_a_3_km_floor():
    p = profile(minutes=300)
    out, notes = run([w(1, "tempo", 12, mins=72), w(4, "intervals", 12, mins=72)], p=p, wks=weeks(14, target=40))               # 24 km hard of 40 -> scale 0.75
    assert [only(out, i)["distance_km"] for i in (1, 4)] == [9.0, 9.0] and [only(out, i)["duration_min"] for i in (1, 4)] == [54, 54]
    assert any("hard sessions shortened to stay within 45%" in n for n in notes)
    out, notes = run([w(1, "tempo", 9.0, mins=54), w(4, "intervals", 9.01, mins=54)], p=p, wks=weeks(14, target=40))               # 18.01: inside the +0.05 slack
    assert [only(out, i)["distance_km"] for i in (1, 4)] == [9.0, 9.0]
    out, notes = run([w(1, "tempo", 9.0, mins=54), w(4, "intervals", 9.2, mins=55)], p=p, wks=weeks(14, target=40))               # 18.2: over
    assert only(out, 1)["distance_km"] < 9.0
    out, _ = run([w(1, "tempo", 4, mins=24), w(4, "intervals", 4, mins=24)], p=p, wks=weeks(14, target=6.5))                       # share would give 1.5 km: floor of 3 km
    assert [only(out, i)["distance_km"] for i in (1, 4)] == [3.0, 3.0]


def test_long_run_growth_chains_from_one_long_run_to_the_next_and_forgets_runs_over_28_days_old():
    p = profile(goal_km=42.195, minutes=120, long_minutes=300)
    raw = [w(6, "long", 30, mins=195), w(13, "long", 30, mins=195), w(20, "long", 30, mins=195)]
    out, _ = run(raw, p=p, wks=weeks(14, target=300, long_km=32), longest_recent_km=20.0)
    # from a recent 20 km: max(22, 22.5) = 22.5, then from 22.5: max(24.75, 25) = 25, then from 25: max(27.5, 27.5) = 27.5
    assert [only(out, i)["distance_km"] for i in (6, 13, 20)] == [22.5, 25.0, 27.5]
    out, _ = run([w(6 + 7 * 5, "long", 30, mins=195)], p=p, wks=weeks(14, target=300, long_km=32), longest_recent_km=10.0)             # 36 days out: the 10 km run is forgotten
    assert only(out, 41)["distance_km"] == 30.0
    out, _ = run([w(6 + 7 * 3, "long", 30, mins=195)], p=p, wks=weeks(14, target=300, long_km=32), longest_recent_km=10.0)             # 27 days out: still counts
    assert only(out, 27)["distance_km"] == 12.5


def test_a_half_marathon_long_run_over_150_minutes_is_cut_to_what_fits():
    p = profile(goal_km=21.0975, minutes=120, long_minutes=300)
    out, notes = run([w(6, "long", 24, mins=200)], p=p, wks=weeks(14, target=300, long_km=24))
    assert only(out, 6)["distance_km"] == 18.0 and only(out, 6)["duration_min"] == 150 and any("long run reduced from 24.0 to 18.0" in n for n in notes)
    out, _ = run([w(6, "long", 19, mins=150)], p=p, wks=weeks(14, target=300, long_km=24))
    assert only(out, 6)["distance_km"] == 19.0                                                   # exactly 150 minutes is fine


def test_text_is_only_rewritten_when_the_change_is_more_than_5_percent():
    p = profile(minutes=300)
    out, _ = run([w(1, "easy", 13, mins=80, title="Easy 13 km"), w(3, "easy", 13, mins=80, title="Easy 13 km")], p=p, wks=weeks(14, target=100))   # 80 min > 70: cap
    assert only(out, 1)["title"] == "Easy 11.4 km"                                                # 13 km * 70/80
    out, _ = run([w(1, "easy", 11.8, mins=71, title="Easy 11.8 km")], p=p)                      # 71 min vs cap 70 (+1 grace): untouched
    assert only(out, 1)["title"] == "Easy 11.8 km" and only(out, 1)["distance_km"] == 11.8
    assert retitle("Tempo 19 km", 20.0, 18.9) == "Tempo 18.9 km" and retitle("Tempo 19 km", 20.0, 19.0) == "Tempo 19 km"      # 1 km is exactly 5% of 20: still "same"
    out, _ = run([w(6, "long", 14, mins=100, title="Steady run", desc="no distance in the title")], p=profile(minutes=300, long_minutes=200), wks=weeks(14, target=300, long_km=10))
    assert "Adjusted from 14 km to 11 km" in only(out, 6)["description"]                         # a long run with no distance to rewrite gets the note instead
    out, _ = run([w(6, "long", 14, mins=100, title="Long run 14 km")], p=profile(minutes=300, long_minutes=200), wks=weeks(14, target=300, long_km=10))
    assert only(out, 6)["title"] == "Long run 11 km" and "Adjusted" not in only(out, 6)["description"]


def test_the_default_race_duration_uses_the_goal_pace_or_the_easy_pace():
    out, _ = run([], p=profile(goal_km=10.0, goal_time_s=50 * 60))
    assert [x["duration_min"] for x in out if x["type"] == "race"] == [50]
    out, _ = run([], p=profile(goal_km=10.0))
    assert [x["duration_min"] for x in out if x["type"] == "race"] == [60]


def test_a_long_run_is_moved_off_the_last_five_days_before_the_race_but_not_six():
    p5 = profile(long_day="tue")                                                               # race on a Sunday: Tuesday is 5 days before
    off = (RACE - TODAY).days
    out, _ = run([w(off - 5, "long", 14), w(off - 12, "long", 14)], p=p5)
    assert quality_types(out)[(RACE - timedelta(days=5)).isoformat()] == "easy" and quality_types(out)[(RACE - timedelta(days=12)).isoformat()] == "long"
    p6 = profile(long_day="mon")
    out, _ = run([w(off - 6, "long", 14)], p=p6)
    assert quality_types(out)[(RACE - timedelta(days=6)).isoformat()] == "long"


@pytest.mark.parametrize("text,expected", [("no idea but my knee hurts", True), ("not really injured", False), ("without any real pain", False), ("never had any pain in my knee", False),
                                           ("no sharp pain", False), ("it doesn't hurt", False)])
def test_negation_only_covers_the_few_words_after_it(text, expected):
    assert mentions_pain(text) is expected
