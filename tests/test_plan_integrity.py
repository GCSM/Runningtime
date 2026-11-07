"""A partial answer from the coach must never leave a plan with empty weeks (reported: weeks after the first few showed 0 km, 0 runs)."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from coach.config import Settings
from coach.db import DB
from coach.errors import UserError
from coach.web import create_app
from test_actions import Env, TODAY
from test_core import FakeStrava, signup, wait_job
from test_scenarios import ReferenceCoach, SETUP


class Partial(ReferenceCoach):
    """Returns only the workouts up to `cutoff` days from today, for the first `bad_calls` calls; then the full plan."""
    def __init__(self, cutoff_days=10, bad_calls=99):
        super().__init__()
        self.cutoff_days, self.bad_calls, self.n, self.feedback = cutoff_days, bad_calls, 0, []

    def plan(self, context, *, mode, feedback=None):
        self.n += 1
        self.feedback.append(feedback)
        out = super().plan(context, mode=mode, feedback=feedback)
        if self.n <= self.bad_calls:
            cut = (date.fromisoformat(context["today"]) + timedelta(days=self.cutoff_days)).isoformat()
            out = {**out, "workouts": [w for w in out["workouts"] if w["date"] <= cut and w["type"] != "race"]}
        return out


def weeks_without_runs(udb, from_day=TODAY):
    have = {w["week_start"] for w in udb.get_workouts(from_day)}
    return [w["week_start"] for w in udb.get_weeks() if w["week_start"] not in have]


def test_a_partial_first_answer_is_retried_with_a_pointer_to_the_missing_weeks(tmp_path):
    coach = Partial(bad_calls=1)
    env = Env(tmp_path, coach=coach, plan=False)
    env.svc.generate_plan()
    assert coach.n == 2 and "no workouts for the weeks starting" in coach.feedback[1][0] and "Cover every week" in coach.feedback[1][0]
    assert weeks_without_runs(env.udb) == []


def test_generating_with_a_coach_that_keeps_returning_part_of_a_plan_saves_nothing(tmp_path):
    env = Env(tmp_path, coach=Partial(), plan=False)
    with pytest.raises(UserError, match="only returned part of the plan"):
        env.svc.generate_plan()
    assert env.udb.get_workouts() == [] and env.udb.get_weeks() == []                      # no hollow plan


def test_a_partial_review_cannot_wipe_the_existing_plan(tmp_path):
    env = Env(tmp_path)
    before = [(w["date"], w["type"], w["distance_km"]) for w in env.udb.get_workouts()]
    env.svc.coach = Partial(cutoff_days=3)                                                 # every call returns just a few days
    env.svc.coach.n = 0
    # the first days are covered, later weeks are not, but the old plan still has workouts there: they are kept
    env.svc.review()
    after = [(w["date"], w["type"]) for w in env.udb.get_workouts()]
    cutoff = (TODAY + timedelta(days=3)).isoformat()
    race_wk = max(w["week_start"] for w in env.udb.get_workouts())
    later = [b for b in before if b[0] > (TODAY + timedelta(days=7)).isoformat() and b[0] < race_wk]       # weeks the coach didn't cover
    assert later and all(any(a[0] == b[0] for a in after) for b in later)                                 # …are exactly as they were
    assert weeks_without_runs(env.udb) == []
    assert any("kept as they were" in m["message"] for m in env.udb.recent_log(4))
    race = [w for w in env.udb.get_workouts() if w["type"] == "race"]
    assert len(race) == 1                                                                   # kept weeks aren't doubled up with new workouts


def test_review_refuses_when_gaps_cannot_be_filled_from_the_old_plan(tmp_path):
    env = Env(tmp_path)
    env.udb.replace_plan_from(TODAY + timedelta(days=14), [], [])                          # simulate the damage: nothing planned after two weeks
    env.svc.coach = Partial(cutoff_days=3)
    with pytest.raises(UserError, match="only returned part of the plan"):
        env.svc.review()
    kept = [w["date"] for w in env.udb.get_workouts()]
    assert kept and max(kept) < (TODAY + timedelta(days=14)).isoformat()                  # nothing was changed by the failed attempt


def test_a_complete_plan_is_untouched_by_the_check(tmp_path):
    coach = ReferenceCoach()
    env = Env(tmp_path, coach=coach, plan=False)
    env.svc.generate_plan()
    assert weeks_without_runs(env.udb) == [] and env.svc.plan_gaps() == []
    env.svc.review()
    assert weeks_without_runs(env.udb) == []


def test_today_offers_a_rebuild_when_weeks_are_empty_and_rebuilding_fixes_it(tmp_path):
    s = Settings(db_path=str(tmp_path / "x.db"), sync_interval_min=0)
    db = DB(s.db_path)
    c = TestClient(create_app(s, db=db, coach=ReferenceCoach(), strava_factory=lambda u: FakeStrava(), clock=lambda: TODAY))
    signup(c)
    c.post("/setup", data={**SETUP, "baseline": "25", "race_date": str(TODAY + timedelta(weeks=10))})
    c.post("/plan/generate"); wait_job(c)
    assert "Part of your plan is empty" not in c.get("/").text
    udb = db.for_user(1)
    with db.conn() as conn:                                                                   # the reported damage: later weeks lost their workouts
        conn.execute("delete from workouts where user_id=1 and date>?", ((TODAY + timedelta(days=9)).isoformat(),))
    page = c.get("/").text
    assert "Part of your plan is empty" in page and "Rebuild my plan" in page and 'action="/review"' in page
    assert udb.get_workouts(TODAY + timedelta(days=30)) == []
    c.post("/review", data={"note": "rebuild"}, headers={"X-Requested-With": "fetch"})
    assert wait_job(c)["status"] == "done"
    assert weeks_without_runs(udb) == [] and "Part of your plan is empty" not in c.get("/").text
