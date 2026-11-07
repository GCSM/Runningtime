"""Simulated athletes living through a whole plan with the real service: the plan is rebuilt every Monday (review) from what they actually ran.

Why: every earlier test looked at one plan at one moment. These follow the plan over weeks and check that what the athlete SEES each Monday stays
sensible: it keeps progressing for someone who follows it, it adapts to someone who can't, it doesn't spiral down or up, and it handles breaks.
(This is where the 'baseline collapses to a quarter after the first week' and 'step-back weeks never happen' defects were found.)
"""
import pathlib
import tempfile
from datetime import date, timedelta

import pytest

from coach import knowledge as K
from coach.config import Settings
from coach.db import DB
from coach.models import DAYS, DayAvail, Profile
from coach.planaudit import TRAIN, audit_shape
from coach.service import CoachService
from coach.skeleton import Week, build_skeleton_info
from test_core import FakeStrava
from test_scenarios import ReferenceCoach, run_activity

START = date(2026, 11, 9)


class Offline(FakeStrava):
    connected = False


def simulate(behaviour, goal=42.195, level="intermediate", weeks=20, base=35.0, days=("tue", "thu", "sat", "sun"), mins=75, long_min=240):
    race = START + timedelta(weeks=weeks, days=6)
    p = Profile(name="x", goal_label="g", goal_km=goal, race_date=race, baseline_km=base, experience=level,
                availability={d: DayAvail(available=d in days, minutes=long_min if d == "sun" else mins) for d in DAYS}, long_run_day="sun")
    clock = {"d": START}
    s = Settings(db_path=str(pathlib.Path(tempfile.mkdtemp()) / "c.db"), sync_interval_min=0)
    db = DB(s.db_path)
    udb = db.for_user(db.create_user("a@example.com", "A", "x"))
    udb.save_profile(p)
    svc = CoachService(udb, s, ReferenceCoach(), Offline(), clock=lambda: clock["d"])
    svc.generate_plan()
    fresh, _ = build_skeleton_info(plan_start=START, today=START, race_date=race, goal_km=goal, baseline_km=base, experience=level, weekly_minutes=p.weekly_minutes,
                                   long_day_minutes=p.long_day_minutes, easy_pace_s_km=390.0, run_days=len(days),
                                   other_day_minutes=[a.minutes for d, a in p.availability.items() if a.available and d != "sun"])
    rows, aid = [], 9000
    for wk in range(weeks):
        mon = START + timedelta(weeks=wk)
        clock["d"] = mon
        if wk:
            svc.review(sync=False)
        planned = [dict(w) for w in udb.get_workouts(mon, mon + timedelta(days=6)) if w["type"] != "race"]
        total = sum(w["distance_km"] for w in planned)
        acts = []
        for w in planned:
            f = behaviour(wk, w, total)
            if f is None or f <= 0:
                continue
            aid += 1
            a = run_activity(aid, date.fromisoformat(w["date"]), km=w["distance_km"] * f, minutes=1)
            a["moving_s"] = int(w["duration_min"] * 60 * f)
            acts.append(a)
        udb.upsert_activities(acts)
        clock["d"] = mon + timedelta(days=7)
        svc.refresh_statuses()
        rows.append({"wk": wk, "planned": round(total, 1), "actual": round(sum(a["distance_m"] for a in acts) / 1000, 1),
                     "planned_long": max([w["distance_km"] for w in planned if w["type"] == "long"] or [0]), "recovery": fresh[wk].recovery if wk < len(fresh) else False,
                     "phase": fresh[wk].phase if wk < len(fresh) else "race", "fresh": fresh[wk].target_km if wk < len(fresh) else 0})
    return rows


def capacity(km):
    return lambda wk, w, total: min(1.0, km / total) if total else 1.0


BEHAVIOURS = {
    "perfect": lambda wk, w, t: 1.0,
    "capacity 25 km": capacity(25.0),
    "over 130%": lambda wk, w, t: 1.3,
    "flu in week 8": lambda wk, w, t: None if wk == 8 else 1.0,
    "off weeks 8-9": lambda wk, w, t: None if wk in (8, 9) else 1.0,
    "off weeks 8-12": lambda wk, w, t: None if 8 <= wk <= 12 else 1.0,
    "never runs the long run": lambda wk, w, t: None if w["type"] == "long" else min(1.0, 9.0 / w["distance_km"]),
    "skips Saturdays": lambda wk, w, t: None if date.fromisoformat(w["date"]).weekday() == 5 else 1.0,
    "capacity 30 km for 8 weeks then fully fit": lambda wk, w, t: min(1.0, 30.0 / t) if wk < 8 else 1.0,
}


@pytest.fixture(scope="module")
def sims():
    return {name: simulate(b) for name, b in BEHAVIOURS.items()}


def planned(rows):
    return [r["planned"] for r in rows]


def test_a_runner_who_follows_the_plan_keeps_progressing_as_originally_designed(sims):
    rows = sims["perfect"]
    build = [r for r in rows if r["phase"] in TRAIN and not r["recovery"]]
    assert all(r["planned"] >= 0.9 * r["fresh"] - 0.5 for r in build), [(r["wk"], r["planned"], r["fresh"]) for r in build if r["planned"] < 0.9 * r["fresh"]]
    peak_week = max(build, key=lambda r: r["fresh"])
    assert max(r["planned"] for r in build) >= 0.93 * peak_week["fresh"]                         # the peak is still reached (it used to erode to ~65% over weekly reviews)
    assert max(r["planned_long"] for r in rows) >= 30.0                                            # and so is the long run
    assert all(r["planned"] <= 1.1 * r["fresh"] + 1 for r in build)                                # never invents more than the plan intended
    assert all(r["planned"] <= 0.85 * rows[i - 1]["planned"] + 0.5 for i, r in enumerate(rows) if r["recovery"] and i)  # step-back weeks happen


def test_the_plan_the_athlete_sees_each_monday_has_a_periodized_shape(sims):
    rows = sims["perfect"]
    weeks = [Week(START + timedelta(weeks=r["wk"]), r["phase"], r["recovery"], False, r["planned"], r["planned_long"]) for r in rows[:-3]]
    assert audit_shape(weeks, 42.195, "intermediate") == []


def test_an_athlete_with_limited_capacity_gets_a_plan_that_settles_near_it_not_one_that_spirals_down(sims):
    rows = sims["capacity 25 km"]
    mid = [r["planned"] for r in rows[4:16] if not r["recovery"]]
    assert min(mid) >= 15.0 and max(mid) <= 25 * 1.8                                              # tracks what they can really do (was ~5 km by the end)
    assert all(r["planned"] >= 4 for r in rows[:-2])
    assert sims["capacity 25 km"][-3]["planned"] >= 12


def test_an_athlete_who_overruns_every_session_is_not_followed_upward_past_the_plans_ceiling(sims):
    rows = sims["over 130%"]
    fresh_peak = max(r["fresh"] for r in rows)
    assert max(r["planned"] for r in rows) <= fresh_peak * 1.05 + 0.5
    assert max(r["planned_long"] for r in rows) <= 32.6                                           # the long-run cap holds even if they run 42 km


def test_one_missed_week_does_not_derail_the_build(sims):
    base, flu = sims["perfect"], sims["flu in week 8"]
    after = [(f["planned"], b["planned"]) for f, b in zip(flu[9:15], base[9:15]) if not f["recovery"]]
    assert all(f >= 0.8 * b for f, b in after), after                                            # back near the original track within a week
    assert all(flu[i]["planned"] >= 0.9 * base[i]["planned"] for i in range(16, 19))               # and fully back before the peak block


def test_a_two_week_break_restarts_lower_then_rebuilds_instead_of_staying_low(sims):
    rows, base = sims["off weeks 8-9"], sims["perfect"]
    pre = max(r["planned"] for r in rows[5:8] if not r["recovery"])
    back = rows[10]["planned"]
    assert 0.4 * pre <= back <= 0.85 * pre, (pre, back)                                           # a cautious restart after two weeks off
    later = [r["planned"] for r in rows[12:18] if not r["recovery"]]
    assert max(later) >= 0.75 * max(r["planned"] for r in base[12:18] if not r["recovery"]) or max(later) >= 0.9 * pre      # and then it builds back up
    assert [r["planned"] for r in rows[10:16] if not r["recovery"]] == sorted(r["planned"] for r in rows[10:16] if not r["recovery"])


def test_a_five_week_break_restarts_cautiously_and_never_below_the_minimum(sims):
    rows = sims["off weeks 8-12"]
    assert all(r["planned"] >= 4 for r in rows[:-2])
    assert rows[13]["planned"] <= 0.75 * rows[7]["planned"] + 0.5 and rows[13]["planned"] >= 0.3 * rows[7]["planned"]


def test_skipping_a_weekday_run_every_week_is_followed_down_gently_not_off_a_cliff(sims):
    rows = sims["skips Saturdays"]
    assert min(r["planned"] for r in rows[:-2]) >= 0.3 * rows[0]["planned"]
    assert all(rows[i + 1]["planned"] >= 0.5 * rows[i]["planned"] for i in range(len(rows) - 3) if not rows[i + 1]["recovery"])        # no week halves the one before it


def test_never_running_the_long_run_still_leaves_a_valid_plan(sims):
    """The plan can only follow what the athlete does (and the ease-off suggestion tells them), but it must stay a valid, non-empty plan."""
    rows = sims["never runs the long run"]
    assert all(r["planned"] >= 4 for r in rows[:-2]) and all(r["planned_long"] <= 32.6 for r in rows)


def test_a_runner_who_can_only_do_30_km_for_two_months_and_then_gets_fit_is_followed_up_again(sims):
    rows = sims["capacity 30 km for 8 weeks then fully fit"]
    assert all(r["planned"] >= 15 for r in rows[2:8] if not r["recovery"])                      # held near what they could do
    assert max(r["planned"] for r in rows[12:18]) >= 40                                          # and builds on once they can run the plan


def test_every_simulated_plan_stays_inside_the_hard_limits(sims):
    for name, rows in sims.items():
        for r in rows:
            assert r["planned"] == r["planned"] and r["planned"] >= 0                       # no NaN
            assert r["planned_long"] <= 32.6, (name, r)
