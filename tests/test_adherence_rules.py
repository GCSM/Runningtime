"""Exact tests for matching Strava runs to planned workouts and for measuring what an athlete currently does (adherence.py).

Mutation testing showed these functions were barely pinned down (about a third of deliberate bugs were caught); the plan is built from these numbers.
"""
from datetime import date, timedelta

import pytest

from coach import adherence as A
from coach.skeleton import monday

TODAY = date(2026, 11, 4)                      # Wednesday


def wk(i, d, km=10.0, mins=None, **kw):
    return {"id": i, "date": d.isoformat(), "distance_km": km, "duration_min": mins, **kw}


def act(i, d, km, sec=None):
    return {"id": i, "date": d.isoformat(), "distance_m": km * 1000, "moving_s": sec if sec is not None else int(km * 360)}


# ----------------------------------------------------------------------------------------------------- matching ----
@pytest.mark.parametrize("ran,status", [(8.0, "completed"), (7.9, "partial"), (10.0, "completed"), (14.0, "completed"), (1.0, "partial")])
def test_eighty_percent_of_the_planned_distance_is_the_line_between_partial_and_completed(ran, status):
    d = TODAY - timedelta(days=2)
    m = A.match_workouts([wk(1, d, 10.0)], [act(5, d, ran)], TODAY)[1]
    assert m["status"] == status and m["actual_km"] == ran and m["activity_id"] == 5


def test_a_workout_planned_by_time_is_judged_by_moving_time():
    d = TODAY - timedelta(days=1)
    plan = {"id": 1, "date": d.isoformat(), "distance_km": None, "duration_min": 50}
    assert A.match_workouts([plan], [act(5, d, 8, sec=2400)], TODAY)[1]["status"] == "completed"       # 40 of 50 minutes = 80%
    assert A.match_workouts([plan], [act(5, d, 8, sec=2340)], TODAY)[1]["status"] == "partial"
    open_ended = {"id": 2, "date": d.isoformat(), "distance_km": None, "duration_min": None}
    assert A.match_workouts([open_ended], [act(5, d, 2)], TODAY)[2]["status"] == "completed"           # nothing to compare with: any run counts


def test_a_run_before_the_workout_day_or_on_another_day_does_not_match():
    d = TODAY - timedelta(days=2)
    out = A.match_workouts([wk(1, d)], [act(5, d - timedelta(days=1), 10), act(6, d + timedelta(days=1), 10)], TODAY)
    assert out[1] == {"status": "missed", "actual_km": None, "activity_id": None}


def test_past_days_without_a_run_are_missed_and_today_or_later_is_still_planned():
    out = A.match_workouts([wk(1, TODAY - timedelta(days=1)), wk(2, TODAY), wk(3, TODAY + timedelta(days=1))], [], TODAY)
    assert [out[i]["status"] for i in (1, 2, 3)] == ["missed", "planned", "planned"]


def test_two_runs_on_one_day_add_up_and_the_longest_is_the_link():
    d = TODAY - timedelta(days=3)
    m = A.match_workouts([wk(1, d, 10.0)], [act(7, d, 4.0), act(8, d, 5.0), act(9, d, 1.5)], TODAY)[1]
    assert m["actual_km"] == 10.5 and m["activity_id"] == 8 and m["status"] == "completed"
    m = A.match_workouts([wk(1, d, 20.0, mins=120)], [act(7, d, 4.0, 1500), act(8, d, 5.0, 1800)], TODAY)[1]
    assert m["status"] == "partial" and m["actual_km"] == 9.0


def test_todays_unfinished_workout_stays_planned_but_finishing_it_completes_it():
    one = A.match_workouts([wk(1, TODAY, 10.0)], [act(5, TODAY, 3.0)], TODAY)[1]
    assert one == {"status": "planned", "actual_km": None, "activity_id": None}                           # a short morning run doesn't block the day
    done = A.match_workouts([wk(1, TODAY, 10.0)], [act(5, TODAY, 3.0), act(6, TODAY, 5.5)], TODAY)[1]
    assert done["status"] == "completed" and done["actual_km"] == 8.5
    future = A.match_workouts([wk(1, TODAY + timedelta(days=1), 10.0)], [act(5, TODAY + timedelta(days=1), 3.0)], TODAY)[1]
    assert future["status"] == "planned"


def test_weekly_km_groups_by_monday():
    acts = [act(1, date(2026, 11, 2), 5), act(2, date(2026, 11, 8), 7), act(3, date(2026, 11, 9), 3)]
    assert A.weekly_km(acts) == {date(2026, 11, 2): 12.0, date(2026, 11, 9): 3.0}


# --------------------------------------------------------------------------------------- current weekly volume ----
def weeks_of_runs(*kms, end=TODAY):
    """One run per week with the given km, oldest first, ending with the last complete week before `end`."""
    last = monday(end) - timedelta(weeks=1)
    return [act(i, last - timedelta(weeks=len(kms) - 1 - i) + timedelta(days=2), km) for i, km in enumerate(kms)]


def test_baseline_is_the_mean_of_the_last_four_complete_weeks():
    assert A.recent_baseline_km(weeks_of_runs(20, 20, 20, 20), TODAY) == 20.0
    assert A.recent_baseline_km(weeks_of_runs(10, 20, 30, 40), TODAY) == pytest.approx(40.0)       # a rising build: the latest week is where the athlete is now
    assert A.recent_baseline_km([], TODAY) is None


def test_one_low_week_among_steady_ones_is_ignored_so_a_plan_does_not_forget_its_progression():
    assert A.recent_baseline_km(weeks_of_runs(36, 36, 26, 37), TODAY) == pytest.approx(37.0)       # a step-back week doesn't pull it down (latest week 37)
    assert A.recent_baseline_km(weeks_of_runs(36, 38, 36, 26), TODAY) == pytest.approx(37.0)       # …and a step-back just finished leaves it at the two best (36, 38)
    assert A.recent_baseline_km(weeks_of_runs(40, 10, 40, 10), TODAY) == pytest.approx(25.0)       # genuinely irregular: plain mean
    assert A.recent_baseline_km(weeks_of_runs(40, 20, 40, 10), TODAY) == pytest.approx(40.0)       # 3 of 4 weeks at least half the best: one off week ignored
    assert A.recent_baseline_km(weeks_of_runs(40, 19, 40, 10), TODAY) == pytest.approx(27.25)      # only 2 of 4: not steady, plain mean
    assert A.recent_baseline_km(weeks_of_runs(0, 0, 0, 12), TODAY) == pytest.approx(12.0)          # just restarted after a layoff: the latest week, not the average of the break
    assert A.recent_baseline_km(weeks_of_runs(12, 0, 0, 0), TODAY) == pytest.approx(3.0)           # still off: plain mean
    assert A.recent_baseline_km(weeks_of_runs(40, 40, 0, 25), TODAY) == pytest.approx(40.0)       # back at 25 after a week off: still a 40 km runner's capacity


def test_weeks_before_the_first_run_are_unknown_not_zero():
    two_weeks = weeks_of_runs(30, 30)                                                              # Strava only goes back two weeks
    assert A.recent_baseline_km(two_weeks, TODAY, fill_km=40.0) == pytest.approx(35.0)              # the two unknown weeks count as the stated 40
    assert A.recent_baseline_km(two_weeks, TODAY, fill_km=0.0) == pytest.approx(30.0)               # stated 0 and 30 km in the last complete week: they are running 30 now
    assert A.recent_baseline_km([act(1, TODAY, 10)], TODAY) is None                                 # only this (incomplete) week so far: no complete week to average
    # zeros AFTER the first run are real
    after = [act(1, monday(TODAY) - timedelta(weeks=4) + timedelta(days=1), 30)]
    assert A.recent_baseline_km(after, TODAY, fill_km=40.0) == pytest.approx(7.5)


def test_easy_pace_is_the_median_of_recent_runs_of_at_least_3_km_and_needs_three_of_them():
    base = TODAY - timedelta(days=3)
    acts = [act(1, base, 5, 1800), act(2, base - timedelta(days=2), 5, 1950), act(3, base - timedelta(days=4), 5, 2100), act(4, base, 2.0, 400)]    # 6:00, 6:30, 7:00; a 2 km run is ignored
    assert A.easy_pace_s_km(acts, TODAY) == 390.0
    assert A.easy_pace_s_km(acts[:2], TODAY) is None                                              # two runs aren't enough
    old = [act(i, TODAY - timedelta(days=29 + i), 5, 1800) for i in range(5)]
    assert A.easy_pace_s_km(old, TODAY) is None                                                   # nothing in the last 28 days
    edge = [act(1, TODAY - timedelta(days=28), 5, 1800), act(2, TODAY - timedelta(days=20), 5, 1800), act(3, TODAY - timedelta(days=10), 5, 1800)]
    assert A.easy_pace_s_km(edge, TODAY) == 360.0                                                 # day 28 is still inside the window
    exact = [act(i, TODAY - timedelta(days=i), 3.0, 1080) for i in (1, 2, 3)]
    assert A.easy_pace_s_km(exact, TODAY) == 360.0                                                # exactly 3 km counts
