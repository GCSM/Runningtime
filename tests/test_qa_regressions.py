"""Regression tests for defects found in the deep QA of plan generation (red-team review, expert review, fuzzing, athlete simulation).
Each test names the failure it prevents."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from coach import knowledge as K
from coach.config import Settings
from coach.db import DB
from coach.errors import UserError
from coach.web import create_app
from test_actions import Env, TODAY
from test_core import FakeStrava, signup, wait_job
from test_scenarios import ReferenceCoach, SETUP, make_service, prof, run_activity


# ------------------------------------------------------------------------------------------------ baselines -------
def test_a_runner_with_only_two_weeks_of_strava_history_is_not_treated_as_running_a_quarter_of_that(tmp_path):
    """Athlete simulation: after week 1 a perfect athlete's next plan collapsed from 34 to 12 km because the missing weeks counted as zero."""
    p = prof(goal_km=42.195, weeks=20, baseline=35.0, days=("tue", "thu", "sat", "sun"), minutes=75, long_minutes=240)
    svc, udb, _ = make_service(tmp_path, p)
    svc.generate_plan()
    runs = [run_activity(100 + i, TODAY - timedelta(days=d), km=km) for i, (d, km) in enumerate([(12, 12), (10, 12), (8, 14), (5, 12), (4, 12)])]
    udb.upsert_activities(runs)
    inp = svc._skeleton(p, runs, monday_of(TODAY), trust_actuals=True)
    assert inp.outlook.start_km >= 25                                          # the stated 35 fills the weeks Strava doesn't know about


def monday_of(d):
    return d - timedelta(days=d.weekday())


def test_a_long_layoff_restarts_from_a_share_of_what_they_did_not_from_the_minimum(tmp_path):
    """Red team: after months off, trust_actuals made the baseline 0 -> an 8 km plan while the coach was told to restart at 50%."""
    p = prof(goal_km=21.0975, weeks=16, baseline=40.0, days=("tue", "thu", "sat", "sun"))
    old = [run_activity(i, TODAY - timedelta(days=60 + i * 3), km=10) for i in range(12)]
    svc, udb, _ = make_service(tmp_path, p, activities=old)
    base, source = svc._baseline(p, old, trust_actuals=True)
    assert source == "restart" and base == pytest.approx(40.0 * K.restart_fraction((TODAY - date.fromisoformat(max(a["date"] for a in old))).days), abs=0.1)
    assert base > K.MIN_BASE_KM
    svc.generate_plan()
    assert udb.kv_get("volume_outlook")["baseline_source"] == "restart"


def test_layoff_beats_a_stale_stated_volume_even_when_history_is_outside_the_90_day_window(tmp_path):
    p = prof(goal_km=21.0975, weeks=14, baseline=40.0)
    old = [run_activity(i, TODAY - timedelta(days=120 + i * 2), km=10) for i in range(10)]
    svc, udb, _ = make_service(tmp_path, p)
    udb.upsert_activities(old)
    svc.generate_plan()
    o = udb.kv_get("volume_outlook")
    assert o["baseline_source"] in ("restart", "stated") and o["start_km"] <= 40.0                  # at no point more than they said


# -------------------------------------------------------------------------------------------- plan state -----------
class Down:
    def plan(self, *a, **k):
        raise RuntimeError("model unavailable")


def test_a_failed_generation_does_not_move_the_plans_origin(tmp_path):
    """Red team: plan_start was saved before the model call, so a failed re-generation shifted every later phase/step-back calculation."""
    env = Env(tmp_path)
    before = env.udb.kv_get("plan_start")
    env.clock["d"] = TODAY + timedelta(days=14)
    env.svc.coach = Down()
    with pytest.raises(RuntimeError):
        env.svc.generate_plan()
    assert env.udb.kv_get("plan_start") == before


def test_restoring_a_skipped_workout_cannot_double_book_its_day(tmp_path):
    """Red team: skip A, move B onto A's day, restore A -> two workouts on one day, both matching the same run."""
    env = Env(tmp_path)
    ws = [x for x in env.udb.get_workouts(TODAY + timedelta(days=1)) if x["status"] == "planned" and x["type"] in ("easy", "tempo", "intervals")]
    a, b = ws[0], ws[2]
    env.svc.skip_workout(a["id"], "")
    env.udb.update_workout(b["id"], date=a["date"], week_start=a["week_start"])               # B ends up on A's day (what 'move' allows onto a skipped day)
    with pytest.raises(UserError, match="now has"):
        env.svc.restore_workout(a["id"])
    assert env.udb.get_workout(a["id"])["status"] == "skipped"
    env.udb.update_workout(b["id"], date=b["date"], week_start=b["week_start"])
    env.svc.restore_workout(a["id"])                                                          # once the day is free it works
    assert env.udb.get_workout(a["id"])["status"] == "planned"


def test_the_background_review_waits_a_day_after_a_failure_and_stops_near_the_race(tmp_path):
    env = Env(tmp_path)
    env.svc.strava.connected = True
    env.svc.strava.fetch_runs = lambda ts: []
    env.udb.kv_delete("last_review_week")
    env.clock["d"] = TODAY + timedelta(days=14)
    calls = []
    real_review = env.svc.review
    env.svc.review = lambda *a, **k: (calls.append(1), (_ for _ in ()).throw(RuntimeError("down")))[1]
    with pytest.raises(RuntimeError):
        env.svc.background_tick()
    assert env.udb.kv_get("auto_review_retry_after") == (env.clock["d"] + timedelta(days=1)).isoformat()
    env.svc.background_tick()                                                                 # same day: not retried (no 30-minute hammering of a failing model)
    assert len(calls) == 1
    env.clock["d"] += timedelta(days=1)
    env.svc.review = real_review
    race = env.udb.get_profile().race_date
    env.clock["d"] = race - timedelta(days=2)
    env.udb.kv_delete("last_review_week")
    env.svc.review = lambda *a, **k: calls.append("late")
    env.svc.background_tick()
    assert "late" not in calls                                                                # within 3 days of the race a review can only fail


# ------------------------------------------------------------------------------------------------ the model -----
def test_the_athletes_real_easy_pace_sets_time_budgets_when_slower_than_the_goal_implies(tmp_path):
    """Red team: a 40:00 10K goal made the app budget at a 5:13 easy pace though the athlete's real easy runs are 7:00."""
    p = prof(goal_km=10.0, weeks=12, baseline=30.0, goal_time_s=40 * 60)
    runs = [run_activity(i, TODAY - timedelta(days=3 + i * 3), km=8, minutes=56) for i in range(6)]            # 7:00/km
    svc, udb, _ = make_service(tmp_path, p, activities=runs)
    paces, basis, pace = svc._paces(p, runs)
    assert basis.startswith("your recent running") and "building toward your goal" in basis        # paces start from current fitness (clamped), not the goal
    assert pace == pytest.approx(420.0) and pace > sum(paces["easy"]) / 2
    inp = svc._skeleton(p, runs, monday_of(TODAY))
    assert any("much slower than" in w for w in inp.outlook.warnings)                                        # and the athlete is told
    assert any("well beyond what your recent runs support" in w and "understates fitness" in w for w in inp.outlook.warnings)   # the goal itself is flagged as ambitious


def test_a_believable_goal_gets_no_pace_warnings(tmp_path):
    p = prof(goal_km=10.0, weeks=12, baseline=30.0, goal_time_s=55 * 60)
    runs = [run_activity(i, TODAY - timedelta(days=3 + i * 3), km=8, minutes=48) for i in range(6)]            # 6:00/km, easy pace
    svc, _, _ = make_service(tmp_path, p, activities=runs)
    inp = svc._skeleton(p, runs, monday_of(TODAY))
    assert not any("slower than" in w or "beyond" in w for w in inp.outlook.warnings)


# ------------------------------------------------------------------------------------------------- units --------
def test_a_miles_athlete_reads_miles_everywhere_the_app_writes_distances(tmp_path):
    """Red team: titles/paces/outlook/warnings were always km while the screens showed miles next to them."""
    p = prof(goal_km=21.0975, weeks=14, baseline=30.0, goal_time_s=110 * 60, days=("tue", "thu", "sat", "sun")).model_copy(update={"units": "mi"})
    svc, udb, coach = make_service(tmp_path, p)
    summary = svc.generate_plan()
    ctx = coach.calls[0][1]
    assert ctx["units"] == "mi" and ctx["training_paces_per_mile"]["easy"].endswith("/mi") and ctx["race_paces_per_mile"]["goal"] == "8:23/mi"
    assert ctx["training_paces_per_km"]["easy"].endswith("/km")                                                 # both are given; the prompt says which to use
    text = udb.kv_get("volume_outlook")["text"]
    assert " mi/week" in text and " km" not in text
    assert "Units:" in __import__("coach.llm", fromlist=["SYSTEM"]).SYSTEM
    assert K.fmt_dist(42.195, "mi", 1) == "26.2 mi" and K.fmt_dist(10, "km") == "10 km" and K.fmt_pace(300, "mi") == "8:03/mi" and K.fmt_pace(300) == "5:00/km"


def test_execution_paces_and_readiness_warnings_use_the_athletes_units():
    from coach import execution as X
    w = {"type": "easy", "distance_km": 10.0, "duration_min": 65, "status": "completed"}
    mi = X.score_run(w, {"distance_m": 10000, "moving_s": 3900}, {"easy": [366, 437]}, "mi")
    assert mi["actual_pace"].endswith("/mi") and mi["target_pace"].endswith("/mi") and mi["actual_pace"] == "10:28/mi"
    ws = K.readiness_warnings(42.195, "intermediate", peak_km=40, peak_long_km=15, limit="time", time_cap_km=40, long_day_minutes=90, weekly_minutes=240, pace_s_km=390,
                              weeks_to_race=20, min_weeks_=16, ideal_low=60, units="mi")
    assert ws and " km" not in " ".join(ws) and " mi" in " ".join(ws)


# ----------------------------------------------------------------------------------------------- execution -------
def test_running_an_easy_day_slower_than_the_band_is_not_penalised_but_too_fast_is():
    from coach import execution as X
    w = {"type": "easy", "distance_km": 8.0, "duration_min": 52, "status": "completed"}
    slow = X.score_run(w, {"distance_m": 8000, "moving_s": 3840}, None)                    # 8:00/km vs a 6:30 plan (band 5:59-7:01)
    assert slow["pace_score"] == 100 and slow["score"] == 100 and "too slow" not in slow["headline"]
    fast = X.score_run(w, {"distance_m": 8000, "moving_s": 2640}, None)
    assert fast["pace_score"] < 60 and "too fast" in fast["headline"]
    t = {"type": "tempo", "distance_km": 8.0, "duration_min": 44, "status": "completed"}
    assert X.score_run(t, {"distance_m": 8000, "moving_s": 3600}, None)["pace_score"] < 100            # a tempo that is too slow still counts against the athlete


def test_too_fast_is_counted_for_easy_runs_only_and_every_suggestion_gets_a_reason():
    from coach import execution as X
    rows = [{"score": 40, "missed": False, "pace_dev": -0.2, "short": False, "easy": False} for _ in range(4)]       # four tempo sessions that were too fast
    o = X.overview(rows)
    assert o["too_fast"] == 0 and o["main_reason"] is None and X.REASON_TEXT[None]
    assert X.overview([{**r, "easy": True} for r in rows])["too_fast"] == 4


# ------------------------------------------------------------------------------------------------ the web --------
def web(tmp_path):
    s = Settings(db_path=str(tmp_path / "w.db"), sync_interval_min=0)
    db = DB(s.db_path)
    app = create_app(s, db=db, coach=ReferenceCoach(), strava_factory=lambda u: FakeStrava(), clock=lambda: TODAY)
    c = TestClient(app)
    signup(c)
    return c, db


@pytest.mark.parametrize("override,message", [
    ({"goal_preset": "", "goal_custom": "2"}, "between 3 km and a marathon"),
    ({"goal_preset": "", "goal_custom": "60"}, "between 3 km and a marathon"),
    ({"goal_preset": "5K", "goal_h": "0", "goal_m": "8", "goal_s": "0"}, "faster than world-record pace"),
    ({"goal_preset": "Marathon", "goal_h": "1", "goal_m": "45", "goal_s": "0"}, "faster than world-record pace"),
    ({"goal_preset": "5K", "goal_h": "2", "goal_m": "0", "goal_s": "0"}, "slower than a walk"),
    ({"av_tue": "", "av_thu": "", "av_sat": ""}, "between 2 and 6 running days"),
    ({"av_mon": "on", "min_mon": "40", "av_wed": "on", "min_wed": "40", "av_fri": "on", "min_fri": "40"}, "between 2 and 6 running days"),
])
def test_goals_nobody_can_plan_for_are_refused_with_a_clear_message(tmp_path, override, message):
    c, db = web(tmp_path)
    form = {**SETUP, "baseline": "20", **override}
    if "av_tue" in override:                                                                  # switching days off: only Sunday left (1 day)
        form = {k: v for k, v in form.items() if k not in ("av_tue", "av_thu", "av_sat")}
    r = c.post("/setup", data=form, follow_redirects=True)
    assert message in r.text.replace("&#39;", "'") and db.for_user(1).get_profile() is None


def test_sensible_goals_at_the_edges_are_accepted(tmp_path):
    c, db = web(tmp_path)
    ok = {**SETUP, "baseline": "20", "goal_preset": "", "goal_custom": "3", "av_tue": "on", "av_sun": "on"}
    ok = {k: v for k, v in ok.items() if k not in ("av_thu", "av_sat")}                       # exactly two run days
    assert "Check your inputs" not in c.post("/setup", data=ok, follow_redirects=True).text and db.for_user(1).get_profile().goal_km == 3.0
    ok2 = {**SETUP, "baseline": "20", "goal_preset": "Marathon", "goal_h": "2", "goal_m": "30", "goal_s": "0", "av_mon": "on", "min_mon": "60", "av_wed": "on", "min_wed": "60"}
    assert "Check your inputs" not in c.post("/setup", data=ok2, follow_redirects=True).text and db.for_user(1).get_profile().goal_time_s == 9000


def test_a_programming_error_in_setup_is_not_reported_to_the_athlete_as_bad_input(tmp_path, monkeypatch):
    """Found while testing: validation referenced an unimported name and the broad `except` told every athlete 'Check your inputs'."""
    c, db = web(tmp_path)
    r = c.post("/setup", data={**SETUP, "baseline": "20"}, follow_redirects=True)
    assert "Check your inputs" not in r.text and db.for_user(1).get_profile() is not None


# -------------------------------------------------------------------------------------------- the prompt ---------
def test_the_prompt_carries_the_rules_the_expert_review_asked_for():
    from coach.llm import SYSTEM
    s = SYSTEM
    for needle in ("at most two hard efforts", "never repeat an identical session more than twice", "average pace *including the walks*", "goal-pace touch",
                   "Threshold-pace work adds up to at most ~10%", "Units:"):
        assert needle in s.replace("\n", " ").replace("  ", " ") or needle.lower() in s.lower().replace("\n", " ").replace("  ", " "), needle
