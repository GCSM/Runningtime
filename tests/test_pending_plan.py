"""Changing goal or schedule hides the old plan behind a pending screen until the new plan exists; a failure is shown with a retry, never papered over."""
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from coach.config import Settings
from coach.db import DB
from coach.web import create_app
from test_actions import TODAY
from test_core import FakeStrava, signup, wait_job
from test_scenarios import ReferenceCoach, SETUP


class Flaky(ReferenceCoach):
    """Fails while `broken` is set (a model that cannot finish), then behaves."""
    broken = False

    def plan(self, ctx, *, mode, feedback=None):
        if self.broken:
            raise RuntimeError("The coach's answer was cut off; try again.")
        return super().plan(ctx, mode=mode, feedback=feedback)


@pytest.fixture
def web(tmp_path):
    s = Settings(db_path=str(tmp_path / "w.db"), sync_interval_min=0, allow_signups=True)
    db = DB(s.db_path)
    coach = Flaky()
    app = create_app(s, db=db, coach=coach, strava_factory=lambda u: FakeStrava(), clock=lambda: TODAY)
    c = TestClient(app)
    signup(c)
    data = {**SETUP, "baseline": "25", "race_date": str(TODAY + timedelta(weeks=12)), "goal_distance": "5", "goal_km": "5"}
    c.post("/setup", data=data)
    c.post("/plan/generate")
    assert wait_job(c)["status"] == "done"
    return c, db, coach, data


def change_goal(c, data):
    return c.post("/setup", data={**data, "goal_distance": "21.0975", "goal_km": "21.0975", "race_date": str(TODAY + timedelta(weeks=16))}, follow_redirects=False)


def test_the_old_plan_is_visible_before_the_change_and_hidden_while_the_new_one_builds(web):
    c, db, coach, data = web
    assert "Building your new plan" not in c.get("/").text and "Building your new plan" not in c.get("/plan").text
    real = coach.plan
    started = []

    def slow(ctx, *, mode, feedback=None):
        started.append(1)
        time.sleep(0.6)
        return real(ctx, mode=mode, feedback=feedback)

    coach.plan = slow
    change_goal(c, data)
    for path in ("/", "/plan", "/progress"):
        page = c.get(path).text
        assert "Building your new plan" in page and "Your coach is working on it" in page, path        # not the old plan
    assert wait_job(c)["status"] == "done"
    for path in ("/", "/plan"):
        assert "Building your new plan" not in c.get(path).text
    assert db.for_user(1).kv_get("plan_pending") is None


def test_a_failed_rebuild_says_what_happened_offers_a_retry_and_does_not_bring_the_old_plan_back(web):
    c, db, coach, data = web
    coach.broken = True
    change_goal(c, data)
    assert wait_job(c)["status"] == "error"
    page = c.get("/plan").text
    assert "couldn&#39;t be built" in page or "couldn't be built" in page
    assert "cut off" in page and 'action="/plan/retry"' in page
    assert "Building your new plan" in c.get("/").text                      # still pending: no old plan
    pend = db.for_user(1).kv_get("plan_pending")
    assert pend and "cut off" in pend["error"]
    coach.broken = False                                                    # retry works once the model can answer
    r = c.post("/plan/retry", headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200
    assert wait_job(c)["status"] == "done"
    assert "Building your new plan" not in c.get("/").text and db.for_user(1).kv_get("plan_pending") is None


def test_retry_with_nothing_pending_is_refused_politely(web):
    c, db, coach, data = web
    r = c.post("/plan/retry", follow_redirects=True)
    assert "Nothing is waiting" in r.text


def test_an_ordinary_review_does_not_hide_the_plan(web):
    c, db, coach, data = web
    c.post("/review", data={"note": "felt great"})
    assert wait_job(c)["status"] == "done"
    assert "Building your new plan" not in c.get("/").text
