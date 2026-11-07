"""'Not feeling 100%': ease the rest of this week (tired / under the weather / busy). Code decides exactly what changes."""
from datetime import date, timedelta

import pytest

from coach import knowledge as K
from coach.errors import UserError
from test_actions import Env, TODAY          # TODAY is a Wednesday


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def rest_of_week(env):
    end = TODAY + timedelta(days=6 - TODAY.weekday())
    return [w for w in env.udb.get_workouts(TODAY, end) if w["type"] != "race"]


def snapshot(env):
    return {w["id"]: dict(w) for w in env.udb.get_workouts()}


def test_tired_trims_the_week_and_makes_every_hard_effort_easy_but_touches_nothing_else(env):
    before = snapshot(env)
    r = env.svc.ease_week("tired")
    after = snapshot(env)
    window = {w["id"] for w in rest_of_week(env)}
    assert window and r["changed"] == len([i for i in window if before[i]["status"] == "planned"])
    for i, b in before.items():
        a = after[i]
        if i in window and b["status"] == "planned":
            assert a["distance_km"] <= b["distance_km"] and (a["type"] not in K.QUALITY_TYPES)
            assert a["distance_km"] >= 0.8 * 0.85 * b["distance_km"] - 0.6
        else:
            assert (a["date"], a["type"], a["distance_km"], a["status"]) == (b["date"], b["type"], b["distance_km"], b["status"])      # later weeks, history, race: untouched
    assert r["km_removed"] > 0


def test_sick_gives_two_rest_days_first_then_an_easier_week(env):
    r = env.svc.ease_week("sick")
    rows = rest_of_week(env)
    first_two = [w for w in rows if (date.fromisoformat(w["date"]) - TODAY).days < 2]
    assert r["rested"] == len(first_two) and all(w["status"] == "skipped" for w in first_two)
    assert all(w["type"] not in K.QUALITY_TYPES for w in rows if w["status"] == "planned")


def test_a_busy_week_shortens_runs_but_keeps_the_shape_and_the_hard_sessions(env):
    before = [w for w in rest_of_week(env) if w["status"] == "planned"]
    hard_before = [w["id"] for w in before if w["type"] in K.QUALITY_TYPES]
    env.svc.ease_week("time")
    after = {w["id"]: w for w in rest_of_week(env)}
    assert all(after[w["id"]]["distance_km"] < w["distance_km"] for w in before if w["distance_km"] >= 3)
    assert [i for i in after if after[i]["type"] in K.QUALITY_TYPES] == hard_before


def test_the_race_is_never_touched_and_unknown_reasons_are_refused(env):
    race_before = dict(next(w for w in env.udb.get_workouts() if w["type"] == "race"))
    env.svc.ease_week("tired")
    race_after = next(w for w in env.udb.get_workouts() if w["type"] == "race")
    assert race_after["distance_km"] == race_before["distance_km"] and race_after["status"] == "planned"
    with pytest.raises(UserError):
        env.svc.ease_week("bored")


def test_nothing_left_this_week_is_said_plainly(env):
    env.clock["d"] = env.udb.get_profile().race_date + timedelta(days=10)
    with pytest.raises(UserError):
        env.svc.ease_week("tired")


def test_the_route_and_the_button_exist(tmp_path):
    from fastapi.testclient import TestClient
    from coach.config import Settings
    from coach.db import DB
    from coach.web import create_app
    from test_core import FakeStrava, signup, wait_job
    from test_scenarios import ReferenceCoach, SETUP
    s = Settings(db_path=str(tmp_path / "w.db"), sync_interval_min=0, allow_signups=True)
    c = TestClient(create_app(s, db=DB(s.db_path), coach=ReferenceCoach(), strava_factory=lambda u: FakeStrava(), clock=lambda: TODAY))
    signup(c)
    c.post("/setup", data={**SETUP, "baseline": "25", "race_date": str(TODAY + timedelta(weeks=12))})
    c.post("/plan/generate")
    assert wait_job(c)["status"] == "done"
    page = c.get("/").text
    assert "Not feeling 100%" in page and 'action="/plan/ease-week"' in page
    r = c.post("/plan/ease-week", data={"reason": "tired"})
    assert r.status_code == 200 and "lighter" in r.text
    r = c.post("/plan/ease-week", data={"reason": "nonsense"})
    assert "Pick how you" in r.text
