"""Skip / move / swap / ease off / run feedback: service behaviour and the web routes."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from coach import knowledge as K
from coach.config import Settings
from coach.db import DB
from coach.errors import UserError
from coach.service import CoachService
from coach.skeleton import monday
from coach.web import create_app
from test_core import FakeStrava, signup, wait_job
from test_scenarios import ReferenceCoach, SETUP, prof, run_activity

TODAY = date(2026, 11, 4)  # Wednesday


class Env:
    """A service on a movable clock, with a generated plan."""
    def __init__(self, tmp_path, profile=None, coach=None, plan=True):
        self.clock = {"d": TODAY}
        s = Settings(db_path=str(tmp_path / "a.db"), sync_interval_min=0)
        self.db = DB(s.db_path)
        self.uid = self.db.create_user("a@example.com", "A", "x")
        self.udb = self.db.for_user(self.uid)
        self.udb.save_profile(profile or prof(goal_km=10.0, weeks=12, baseline=30, days=("mon", "tue", "thu", "sat", "sun"), minutes=75, long_minutes=120))
        self.coach = coach or ReferenceCoach()
        self.svc = CoachService(self.udb, s, self.coach, FakeStrava(), clock=lambda: self.clock["d"])
        if plan:
            self.svc.generate_plan()

    def w(self, **match):
        return next(x for x in self.udb.get_workouts() if all(x[k] == v for k, v in match.items()))

    def future(self, typ=None, after=TODAY, status="planned"):
        return [x for x in self.udb.get_workouts(after + timedelta(days=1)) if x["status"] == status and (typ is None or x["type"] == typ)]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# =========================================================== skip =====================================================
def test_skip_marks_the_workout_keeps_a_reason_and_logs_it(env):
    w = env.future("easy")[0]
    env.svc.skip_workout(w["id"], "  work trip  ")
    got = env.udb.get_workout(w["id"])
    assert got["status"] == "skipped" and got["note"] == "work trip"
    assert "work trip" in env.udb.recent_log(1)[0]["message"]


def test_a_skip_survives_status_refresh_and_the_day_passing(env):
    w = env.future("easy")[0]
    env.svc.skip_workout(w["id"])
    env.svc.refresh_statuses()
    assert env.udb.get_workout(w["id"])["status"] == "skipped"
    env.clock["d"] = date.fromisoformat(w["date"]) + timedelta(days=3)           # the day is long gone: a skip must not become "missed"
    env.svc.refresh_statuses()
    assert env.udb.get_workout(w["id"])["status"] == "skipped"


def test_running_anyway_overrides_a_skip(env):
    w = env.future("easy")[0]
    env.svc.skip_workout(w["id"])
    env.udb.upsert_activities([run_activity(77, date.fromisoformat(w["date"]), km=w["distance_km"], minutes=40)])
    env.clock["d"] = date.fromisoformat(w["date"])
    env.svc.refresh_statuses()
    assert env.udb.get_workout(w["id"])["status"] == "completed"


def test_skips_are_neutral_for_streak_and_consistency(env):
    past = [x for x in env.udb.get_workouts() if x["type"] != "race"]
    a, b, c = past[0], past[1], past[2]
    env.clock["d"] = date.fromisoformat(c["date"]) + timedelta(days=1)
    for x, status in ((a, "completed"), (b, "skipped"), (c, "completed")):
        env.udb.update_workout(x["id"], status=status)
    assert env.svc.streak() == 2                                           # skip is skipped over, not counted and not a break
    assert env.svc.progress()["stats"]["scheduled"] == 2                   # …and isn't a "missed" in consistency


def test_cannot_skip_the_race_a_finished_run_or_a_past_day_or_someone_elses(env, tmp_path):
    race = env.w(type="race")
    with pytest.raises(UserError, match="race"):
        env.svc.skip_workout(race["id"])
    done = env.future("easy")[0]
    env.udb.update_workout(done["id"], status="completed")
    with pytest.raises(UserError, match="upcoming"):
        env.svc.skip_workout(done["id"])
    env.clock["d"] = date.fromisoformat(env.future("long")[0]["date"]) + timedelta(days=1)
    gone = env.future("easy", after=TODAY)[0]
    env.udb.update_workout(gone["id"], date=(env.clock["d"] - timedelta(days=2)).isoformat())
    with pytest.raises(UserError, match="passed"):
        env.svc.skip_workout(gone["id"])
    other = env.db.create_user("b@example.com", "B", "x")                  # another account cannot touch these workouts
    svc2 = CoachService(env.db.for_user(other), Settings(db_path=env.db.path), env.coach, FakeStrava(), clock=lambda: TODAY)
    with pytest.raises(UserError, match="wasn't found"):
        svc2.skip_workout(env.future("long")[0]["id"])


def test_restore_undoes_a_skip(env):
    w = env.future("easy")[0]
    env.svc.skip_workout(w["id"], "x")
    env.svc.restore_workout(w["id"])
    assert env.udb.get_workout(w["id"])["status"] == "planned" and env.udb.get_workout(w["id"])["note"] == ""
    with pytest.raises(UserError, match="isn't skipped"):
        env.svc.restore_workout(w["id"])


def test_a_replan_does_not_put_a_run_back_on_a_skipped_day(env):
    w = env.future("easy")[0]
    env.svc.skip_workout(w["id"])
    env.svc.review("")
    assert w["date"] in env.coach.calls[-1][1]["blocked_dates"]
    same_day = [x for x in env.udb.get_workouts() if x["date"] == w["date"]]
    assert [x["status"] for x in same_day] == ["skipped"]                    # the skip stays; nothing new was scheduled over it


# =========================================================== move / swap ===============================================
def free_usual_day(env, after_days=2):
    occ = {w["date"] for w in env.udb.get_workouts()}
    p = env.udb.get_profile()
    d = TODAY + timedelta(days=after_days)
    while d < p.race_date:
        if d.isoformat() not in occ and p.availability[["mon", "tue", "wed", "thu", "fri", "sat", "sun"][d.weekday()]].available:
            return d
        d += timedelta(days=1)
    raise AssertionError("no free usual day")


def free_unusual_day(env):
    occ = {w["date"] for w in env.udb.get_workouts()}
    d = TODAY + timedelta(days=1)
    while d.isoformat() in occ or d.weekday() not in (2, 4):                  # Wed/Fri aren't running days in this profile
        d += timedelta(days=1)
    return d


def test_move_changes_date_and_week_and_remembers_where_it_came_from(env):
    w = env.future("easy")[0]
    target = free_usual_day(env, after_days=9)                                # a day in a later week
    env.svc.move_workout(w["id"], target)
    got = env.udb.get_workout(w["id"])
    assert got["date"] == target.isoformat() and got["week_start"] == monday(target).isoformat() and got["moved_from"] == w["date"]
    env.svc.move_workout(w["id"], free_usual_day(env, after_days=3))          # moving twice keeps the ORIGINAL date
    assert env.udb.get_workout(w["id"])["moved_from"] == w["date"]


def test_move_warns_about_unusual_days_and_hard_sessions_next_to_each_other(env):
    w = env.future("easy")[0]
    unusual = free_unusual_day(env)
    warns = env.svc.move_workout(w["id"], unusual)
    assert any("isn't one of your usual running days" in x for x in warns)
    long_run = next(x for x in env.future("long") if date.fromisoformat(x["date"]) > TODAY + timedelta(days=2))
    quality = next((x for x in env.future() if x["type"] in K.QUALITY_TYPES), None)
    if quality:
        beside = date.fromisoformat(long_run["date"]) - timedelta(days=1)
        occ = {x["date"] for x in env.udb.get_workouts()}
        if beside.isoformat() not in occ:
            assert any("another hard session" in x for x in env.svc.move_workout(quality["id"], beside))


def test_move_rules(env):
    w = env.future("easy")[0]
    with pytest.raises(UserError, match="later day"):
        env.svc.move_workout(w["id"], TODAY - timedelta(days=1))
    with pytest.raises(UserError, match="before the race"):
        env.svc.move_workout(w["id"], env.udb.get_profile().race_date)
    other = next(x for x in env.future() if x["id"] != w["id"])
    with pytest.raises(UserError, match="Swap them instead"):
        env.svc.move_workout(w["id"], date.fromisoformat(other["date"]))


def test_reschedule_swaps_when_the_day_is_taken_and_moves_when_free(env):
    a = env.future("easy")[0]
    b = next(x for x in env.future() if x["id"] != a["id"] and x["type"] == "easy")
    what, _ = env.svc.reschedule(a["id"], date.fromisoformat(b["date"]))
    assert what.startswith("Swapped with")
    assert env.udb.get_workout(a["id"])["date"] == b["date"] and env.udb.get_workout(b["id"])["date"] == a["date"]
    what, _ = env.svc.reschedule(a["id"], free_usual_day(env, after_days=5))
    assert what.startswith("Moved to")


def test_swap_validation(env):
    a = env.future("easy")[0]
    with pytest.raises(UserError, match="two different"):
        env.svc.swap_workouts(a["id"], a["id"])
    with pytest.raises(UserError, match="race"):
        env.svc.swap_workouts(a["id"], env.w(type="race")["id"])


def test_move_options_list_free_and_swap_days_within_the_plan(env):
    w = env.future("easy")[0]
    opts = env.svc.move_options(w["id"])
    dates = [o["date"] for o in opts]
    assert w["date"] not in dates and all(TODAY.isoformat() <= d < env.udb.get_profile().race_date.isoformat() for d in dates)
    assert any(o["swap_with"] for o in opts) and any(o["swap_with"] is None for o in opts)
    assert all(isinstance(o["usual"], bool) for o in opts)
    assert env.w(type="race")["date"] not in dates


# =========================================================== ease off ==================================================
def test_ease_off_turns_hard_sessions_easy_and_trims_volume_for_a_week(env):
    window = env.udb.get_workouts(TODAY, TODAY + timedelta(days=7))
    before = {w["id"]: w for w in window if w["status"] == "planned"}
    n = env.svc.ease_off()
    assert n == len(before) and n > 0
    for wid, old in before.items():
        new = env.udb.get_workout(wid)
        assert new["type"] not in K.QUALITY_TYPES and new["distance_km"] <= old["distance_km"]
        if old["type"] == "long":
            assert new["distance_km"] == pytest.approx(old["distance_km"] * 0.75, abs=0.06)
    assert env.udb.get_workout(env.w(type="race")["id"])["type"] == "race"
    assert env.svc.pain_until() == TODAY + timedelta(days=7)
    beyond = [w for w in env.udb.get_workouts(TODAY + timedelta(days=9)) if w["type"] in K.QUALITY_TYPES]
    assert beyond                                                            # outside the window the plan is untouched


def test_pain_window_keeps_a_later_review_from_putting_hard_work_back(env):
    env.svc.ease_off()
    env.svc.review("")
    near = [w for w in env.udb.get_workouts(TODAY, TODAY + timedelta(days=6)) if w["status"] == "planned"]
    assert near and all(w["type"] not in K.QUALITY_TYPES for w in near)
    env.clock["d"] = TODAY + timedelta(days=8)                               # the window has passed
    assert env.svc.pain_until() is None


# =========================================================== feedback ==================================================
def add_run(env, day=TODAY - timedelta(days=1), aid=501, km=8.0):
    env.udb.upsert_activities([run_activity(aid, day, km=km, minutes=48)])
    return aid


def test_feedback_is_saved_validated_and_scoped_to_the_users_own_runs(env):
    aid = add_run(env)
    assert env.svc.save_feedback(aid, "good", 4, " felt smooth ") == {"pain": False, "eased": 0}
    fb = env.udb.get_feedback(aid)
    assert (fb["feeling"], fb["rpe"], fb["note"]) == ("good", 4, "felt smooth")
    env.svc.save_feedback(aid, "hard", 8, "")                                 # re-rating replaces
    assert env.udb.get_feedback(aid)["feeling"] == "hard" and len(env.udb.feedback_map()) == 1
    with pytest.raises(UserError, match="Pick how"):
        env.svc.save_feedback(aid, "meh")
    with pytest.raises(UserError, match="1 to 10"):
        env.svc.save_feedback(aid, "good", 11)
    with pytest.raises(UserError, match="wasn't found"):
        env.svc.save_feedback(99999, "good")
    other = env.db.create_user("b@example.com", "B", "x")
    with pytest.raises(UserError, match="wasn't found"):                    # another user's activity id is invisible
        CoachService(env.db.for_user(other), Settings(db_path=env.db.path), env.coach, FakeStrava(), clock=lambda: TODAY).save_feedback(aid, "good")


def test_pain_feedback_eases_the_plan_and_logs_advice(env):
    aid = add_run(env)
    r = env.svc.save_feedback(aid, "pain", 6, "")
    assert r["pain"] and r["eased"] > 0 and env.svc.pain_until()
    assert "physio or doctor" in env.udb.recent_log(1)[0]["message"]
    aid2 = add_run(env, day=TODAY, aid=502)
    env.clock["d"] = TODAY + timedelta(days=10)                              # let the first window lapse
    r2 = env.svc.save_feedback(aid2, "good", 3, "my knee hurts a bit on the downhills")   # pain mentioned in the note counts too
    assert r2["pain"] is True


def test_pending_feedback_offers_the_latest_unrated_recent_run(env):
    assert env.svc.pending_feedback() is None
    old = add_run(env, day=TODAY - timedelta(days=6), aid=601)               # too old to ask about
    assert env.svc.pending_feedback() is None
    a = add_run(env, day=TODAY - timedelta(days=2), aid=602)
    b = add_run(env, day=TODAY - timedelta(days=1), aid=603)
    assert env.svc.pending_feedback()["activity"]["id"] == b
    env.svc.save_feedback(b, "easy")
    assert env.svc.pending_feedback()["activity"]["id"] == a
    env.svc.save_feedback(a, "easy")
    assert env.svc.pending_feedback() is None and old


def test_fatigue_signal_needs_two_hard_easy_runs(env):
    ws = [w for w in env.udb.get_workouts(end=TODAY + timedelta(days=30)) if w["type"] in ("easy", "long")][:4]
    env.clock["d"] = date.fromisoformat(ws[-1]["date"]) + timedelta(days=1)
    for i, w in enumerate(ws):
        env.udb.upsert_activities([run_activity(700 + i, date.fromisoformat(w["date"]), km=w["distance_km"])])
        env.udb.update_workout(w["id"], status="completed", activity_id=700 + i)
    assert env.svc.fatigue_signal() is False
    env.svc.save_feedback(700, "hard", 8)
    assert env.svc.fatigue_signal() is False                                  # one hard run isn't a pattern
    env.svc.save_feedback(701, "good", 8)                                     # RPE ≥ 7 counts even when it was "good"
    assert env.svc.fatigue_signal() is True


def test_review_context_carries_feedback_skips_moves_and_fatigue(env):
    w = env.future("easy")[0]
    env.svc.skip_workout(w["id"], "sick kid")
    m = env.future("easy")[1]
    env.svc.move_workout(m["id"], free_usual_day(env, after_days=4))
    done = env.udb.get_workouts(end=TODAY + timedelta(days=30))[0]
    aid = add_run(env, day=date.fromisoformat(done["date"]), aid=800)
    env.udb.update_workout(done["id"], status="completed", activity_id=aid)
    env.clock["d"] = date.fromisoformat(done["date"]) + timedelta(days=1)
    env.svc.save_feedback(aid, "hard", 9, "legs heavy")
    env.svc.review("")
    ctx = env.coach.calls[-1][1]
    changes = {c["date"]: c for c in ctx["athlete_changes_last_14_days"]}
    assert any(c["change"] == "skipped" and c.get("reason") == "sick kid" for c in changes.values())
    assert any(c["change"].startswith("moved from") for c in changes.values())
    flat = [r for wk in ctx["last_4_weeks_review"] for r in wk["workouts"]]
    assert any(r.get("felt") == "hard" and r.get("rpe") == 9 and r.get("athlete_note") == "legs heavy" for r in flat)
    assert "fatigue_signal" in ctx


# =========================================================== web ======================================================
def web(tmp_path):
    s = Settings(db_path=str(tmp_path / "w.db"), sync_interval_min=0)
    db = DB(s.db_path)
    clock = lambda: TODAY  # noqa: E731
    app = create_app(s, db=db, coach=ReferenceCoach(), strava_factory=lambda u: FakeStrava(), clock=clock)
    c = TestClient(app)
    signup(c)
    c.post("/setup", data={**SETUP, "baseline": "25"})
    c.post("/plan/generate")
    assert wait_job(c)["status"] == "done"
    return c, db


def first(db, typ="easy", uid=1):
    return next(w for w in db.for_user(uid).get_workouts(TODAY + timedelta(days=1)) if w["type"] == typ and w["status"] == "planned")


def test_web_skip_move_and_undo(tmp_path):
    c, db = web(tmp_path)
    w = first(db)
    r = c.post(f"/workout/{w['id']}/skip", data={"reason": "tired", "next": "/plan"}, follow_redirects=True)
    assert "Skipped" in r.text and db.for_user(1).get_workout(w["id"])["status"] == "skipped"
    assert "Undo skip" in c.get("/plan").text
    c.post(f"/workout/{w['id']}/restore", data={"next": "/plan"})
    assert db.for_user(1).get_workout(w["id"])["status"] == "planned"
    target = TODAY + timedelta(days=20)
    occupied = {x["date"] for x in db.for_user(1).get_workouts()}
    while target.isoformat() in occupied:
        target += timedelta(days=1)
    r = c.post(f"/workout/{w['id']}/move", data={"to_date": target.isoformat(), "next": "/plan"}, follow_redirects=True)
    assert "Moved to" in r.text and db.for_user(1).get_workout(w["id"])["date"] == target.isoformat()
    bad = c.post(f"/workout/{w['id']}/move", data={"to_date": "not-a-date"}, follow_redirects=True)
    assert "Pick a date" in bad.text


def test_web_pages_show_the_new_controls(tmp_path):
    c, db = web(tmp_path)
    plan = c.get("/plan").text
    assert 'name="to_date"' in plan and "Moving onto a day that already has a run swaps the two" in plan
    # a planned run today appears on Today with "Can't run today?"
    today_w = next((w for w in db.for_user(1).get_workouts(TODAY, TODAY) if w["status"] == "planned"), None)
    if today_w:
        assert "Can’t run today?" in c.get("/").text


def test_web_feedback_flow_including_pain(tmp_path):
    c, db = web(tmp_path)
    db.for_user(1).upsert_activities([run_activity(901, TODAY - timedelta(days=1), km=8)])
    page = c.get("/").text
    assert 'id="feedback"' in page and 'name="feeling"' in page and 'name="rpe"' in page
    r = c.post("/feedback", data={"activity_id": 901, "feeling": "good", "rpe": "4", "note": "", "next": "/"}, follow_redirects=True)
    assert "Thanks, noted" in r.text and 'id="feedback"' not in r.text
    db.for_user(1).upsert_activities([run_activity(902, TODAY, km=5)])
    r = c.post("/feedback", data={"activity_id": 902, "feeling": "pain", "rpe": "6", "note": "", "next": "/"}, follow_redirects=True)
    assert "eased off" in r.text and db.for_user(1).kv_get("pain_until")
    assert "physio or doctor" in c.get("/").text                                # the advice is the latest coach note


def test_next_parameter_cannot_redirect_off_site(tmp_path):
    c, db = web(tmp_path)
    w = first(db)
    for evil in ("//evil.example/x", "https://evil.example", "/ok?x=1", "\\\\evil"):
        r = c.post(f"/workout/{w['id']}/skip", data={"next": evil}, follow_redirects=False)
        loc = r.headers["location"]
        assert loc.startswith("/") and not loc.startswith("//") and "evil" not in loc, (evil, loc)
        c.post(f"/workout/{w['id']}/restore", data={"next": "/"})


def test_another_user_cannot_act_on_my_workouts_over_http(tmp_path):
    c, db = web(tmp_path)
    w = first(db)
    b = TestClient(c.app)
    signup(b, "b@example.com", "B")
    b.post("/setup", data={**SETUP, "baseline": "25"})                      # so the landing page can show the error message
    r = b.post(f"/workout/{w['id']}/skip", data={"next": "/"}, follow_redirects=True)
    assert "wasn&#39;t found" in r.text and db.for_user(1).get_workout(w["id"])["status"] == "planned"
    assert b.post("/feedback", data={"activity_id": 12345, "feeling": "good", "next": "/"}, follow_redirects=True).status_code == 200
    assert db.for_user(2).feedback_map() == {}
