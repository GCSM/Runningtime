"""Execution score (distance + pace vs the plan) and the 'make it slightly easier' suggestion after two weeks under 70%."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from coach import execution as X
from coach.config import Settings
from coach.db import DB
from coach.web import create_app
from test_actions import Env, TODAY
from test_core import FakeStrava, signup, wait_job
from test_scenarios import ReferenceCoach, SETUP, run_activity

PACES = {"easy": [366, 437], "threshold": [300, 300]}          # 6:06-7:17 /km easy


def wk(kind="easy", km=10.0, mins=65, status="completed"):
    return {"type": kind, "distance_km": km, "duration_min": mins, "status": status}


def act(km, sec):
    return {"distance_m": km * 1000, "moving_s": sec}


# ------------------------------------------------------------------------------------------------- the score -----
@pytest.mark.parametrize("ratio,expected", [(1.0, 100), (0.95, 90), (1.05, 90), (0.90, 80), (1.10, 80), (0.75, 50), (1.25, 50), (0.5, 0), (2.0, 0)])
def test_distance_score_loses_two_points_per_percent_over_or_under(ratio, expected):
    assert round(X._distance_score(10.0, 10.0 * ratio)) == expected


def test_pace_inside_the_easy_band_is_perfect_and_outside_it_falls_to_zero_at_15_percent():
    band = (366.0, 437.0)
    for p in (366, 400, 437):
        assert X._pace_score(p, band) == (100.0, 0.0)
    s, dev = X._pace_score(330, band)                                  # 9.8% too fast
    assert round(s) == 34 and dev < 0
    s, dev = X._pace_score(437 * 1.075, band)                          # 7.5% too slow
    assert round(s) == 50 and dev > 0
    assert X._pace_score(300, band)[0] == 0 and X._pace_score(600, band)[0] == 0


def test_a_run_on_target_scores_100_and_explains_nothing_to_fix():
    r = X.score_run(wk(), act(10, 3900), PACES)                         # 6:30/km: inside the band
    assert r["score"] == 100 and r["label"] == "Nailed it" and r["headline"] == "right on target" and r["notes"] == []
    assert r["actual_pace"] == "6:30/km" and r["target_pace"] == "6:06/km–7:17/km" and r["distance_score"] == 100 and r["pace_score"] == 100


def test_easy_runs_that_are_too_fast_or_too_short_are_marked_down_and_say_why():
    fast = X.score_run(wk(), act(10, 3300), PACES)                      # 5:30/km on an easy day
    assert fast["pace_score"] < 40 and fast["score"] == round((100 + fast["pace_score"]) / 2) and "too fast" in fast["headline"] and fast["pace_dev"] < 0
    short = X.score_run(wk(), act(7, 2730), PACES)                      # 30% short, pace in band
    assert short["distance_score"] == 40 and short["score"] == 70 and "30% short" in short["headline"] and short["label"] == "Solid"
    both = X.score_run(wk(), act(7, 2310), PACES)                       # short and too fast
    assert both["score"] < 50 and both["label"] == "Way off" and "short" in both["headline"] and "too fast" in both["headline"]
    over = X.score_run(wk(), act(12, 4500), PACES)
    assert "20% long" in over["headline"] and over["distance_score"] == 60


def test_quality_sessions_are_judged_against_the_plans_own_session_pace_with_a_tolerance():
    t = wk("tempo", 12.0, 66)                                           # 5:30/km implied average
    assert X.score_run(t, act(12, 3960), PACES)["pace_score"] == 100
    assert X.score_run(t, act(12, 3960 * 1.04), PACES)["pace_score"] == 100        # within 5%
    slow = X.score_run(t, act(12, 3960 * 1.12), PACES)
    assert 0 < slow["pace_score"] < 70 and slow["pace_dev"] > 0
    assert X.target_band(wk("cross"), PACES) is None and X.target_band({"type": "tempo"}, PACES) is None


def test_without_paces_or_timing_the_score_falls_back_sensibly():
    easy_no_paces = X.score_run(wk(), act(10, 3900), None)                # implied 6:30 ±8%
    assert easy_no_paces["pace_score"] == 100 and easy_no_paces["target_pace"] == "5:59/km–7:01/km"
    no_time = X.score_run(wk(), {"distance_m": 9000, "moving_s": 0}, PACES)
    assert no_time["pace_score"] is None and no_time["score"] == 80 and no_time["actual_pace"] is None
    cross = X.score_run(wk("cross"), act(10, 3000), PACES)
    assert cross["pace_score"] is None and cross["score"] == 100


def test_nothing_to_score_for_unmatched_skipped_or_planned_workouts():
    assert X.score_run(wk(status="planned"), act(10, 3900), PACES) is None
    assert X.score_run(wk(status="skipped"), act(10, 3900), PACES) is None
    assert X.score_run(wk(status="missed"), None, PACES) is None
    assert X.score_run(wk(), act(0, 0), PACES) is None
    assert X.score_run({**wk(), "distance_km": 0}, act(5, 1800), PACES) is None
    assert [X.label(s) for s in (100, 85, 84, 70, 69, 50, 49, 0)] == ["Nailed it", "Nailed it", "Solid", "Solid", "Off target", "Off target", "Way off", "Way off"]


def test_overview_counts_reasons_and_averages_missed_runs_as_zero():
    rows = [{"score": 90, "missed": False, "pace_dev": 0.0, "short": False}, {"score": 40, "missed": False, "pace_dev": -0.1, "short": False},
            {"score": 0, "missed": True, "pace_dev": None, "short": False}, {"score": 60, "missed": False, "pace_dev": -0.05, "short": True}]
    o = X.overview(rows)
    assert o == {"avg": 48, "n": 4, "under": 3, "missed": 1, "too_fast": 2, "short": 1, "main_reason": "too_fast"}
    assert X.overview([]) is None
    assert X.overview([{"score": 95, "missed": False, "pace_dev": 0.0, "short": False}])["main_reason"] is None


# ---------------------------------------------------------------------------------- over real plans and weeks ----
@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def run_the_plan(env, until, effort=1.0, pace_factor=1.0, skip_every=0):
    """Advance the clock to `until`, feeding a run for every planned workout before it, scaled by effort (distance) and pace_factor (time)."""
    env.clock["d"] = until
    acts, n = [], 1000
    for w in env.udb.get_workouts(end=until - timedelta(days=1)):
        if w["type"] == "race" or w["status"] == "skipped":
            continue
        n += 1
        if skip_every and n % skip_every == 0:
            continue
        km = w["distance_km"] * effort
        acts.append(run_activity(n, date.fromisoformat(w["date"]), km=km, minutes=1))
        acts[-1]["moving_s"] = int(w["duration_min"] * 60 * effort * pace_factor)
    env.udb.upsert_activities(acts)
    env.svc.refresh_statuses()


THREE_WEEKS = TODAY + timedelta(days=21)


def test_each_completed_workout_gets_a_score_in_the_plan_and_on_the_dashboard(env):
    run_the_plan(env, THREE_WEEKS)
    full = [w for wk_ in env.svc.full_plan() for w in wk_["workouts"] if w["status"] == "completed"]
    assert full and all(w["execution"] and 0 <= w["execution"]["score"] <= 100 for w in full)
    assert all(w["execution"] is None for wk_ in env.svc.full_plan() for w in wk_["workouts"] if w["status"] == "planned")
    d = env.svc.dashboard()
    done_this_week = [w for w in d["week_workouts"] if w["status"] in ("completed", "partial")]
    assert done_this_week and all(w["execution"] for w in done_this_week)
    assert env.svc.progress()["stats"]["execution"]["n"] >= 4


def test_regular_low_execution_after_two_weeks_triggers_the_suggestion_with_a_reason(env):
    run_the_plan(env, THREE_WEEKS, effort=0.65)                        # runs ~35% short, every time
    s = env.svc.ease_suggestion()
    assert s and s["avg"] < 70 and s["under"] * 2 >= s["n"] >= 4 and s["main_reason"] == "short" and "short" in s["reason_text"] and s["threshold"] == 70


def test_good_execution_never_triggers_it(env):
    run_the_plan(env, THREE_WEEKS, effort=1.0)
    assert env.svc.execution_overview()["avg"] >= 90 and env.svc.ease_suggestion() is None


def test_missed_runs_count_against_the_runner_but_skipped_runs_do_not(env):
    run_the_plan(env, THREE_WEEKS, skip_every=2)                       # every second run never happens
    ov = env.svc.execution_overview()
    assert ov["missed"] >= 3 and ov["avg"] < 70 and env.svc.ease_suggestion()
    env2 = env
    for w in env2.udb.get_workouts(THREE_WEEKS - timedelta(days=14), THREE_WEEKS - timedelta(days=1)):
        if w["status"] == "missed":
            env2.udb.update_workout(w["id"], status="skipped")        # the athlete chose to skip them instead
    ov2 = env2.svc.execution_overview()
    assert ov2["missed"] == 0 and ov2["avg"] >= 90
    assert env2.svc.ease_suggestion() is None


def test_one_bad_run_or_too_few_runs_is_not_regular(env):
    run_the_plan(env, THREE_WEEKS)
    ws = [w for w in env.udb.get_workouts(THREE_WEEKS - timedelta(days=14), THREE_WEEKS - timedelta(days=1)) if w["status"] == "completed"]
    a = env.udb.get_activity(ws[0]["activity_id"])
    env.udb.upsert_activities([{**a, "distance_m": a["distance_m"] * 0.4}])
    env.svc.refresh_statuses()
    assert env.svc.execution_overview()["under"] >= 1 and env.svc.ease_suggestion() is None        # one run in several
    few = env
    for w in few.udb.get_workouts(THREE_WEEKS - timedelta(days=14), THREE_WEEKS - timedelta(days=1))[3:]:
        few.udb.update_workout(w["id"], status="skipped")
    assert few.svc.ease_suggestion() is None


def test_it_waits_two_weeks_and_stays_quiet_near_the_race_in_pain_mode_and_after_a_dismissal(env):
    run_the_plan(env, TODAY + timedelta(days=10), effort=0.6)
    assert env.svc.ease_suggestion() is None                           # only 10 days on the plan
    run_the_plan(env, THREE_WEEKS, effort=0.6)
    assert env.svc.ease_suggestion()
    env.svc.dismiss_ease_suggestion()
    assert env.svc.ease_suggestion() is None
    quiet_until = THREE_WEEKS + timedelta(days=7)
    assert env.udb.kv_get("ease_plan_quiet_until") == quiet_until.isoformat()
    env.clock["d"] = quiet_until - timedelta(days=1)
    assert env.svc.ease_suggestion() is None                           # still quiet the day before
    env.clock["d"] = quiet_until
    assert env.svc.ease_suggestion()                                   # and back once the week is up (the runs since were never made)
    env.clock["d"] = THREE_WEEKS
    env.udb.kv_delete("ease_plan_quiet_until")
    assert env.svc.ease_suggestion()
    env.svc.ease_off()
    assert env.svc.ease_suggestion() is None                           # pain mode already eased things


def test_ease_plan_trims_distance_keeps_one_hard_session_per_week_and_protects_the_race_and_history(env):
    run_the_plan(env, THREE_WEEKS, effort=0.65)
    end = THREE_WEEKS + timedelta(days=13)
    before = {w["id"]: dict(w) for w in env.udb.get_workouts(THREE_WEEKS, end + timedelta(days=60))}
    r = env.svc.ease_plan()
    after = {w["id"]: w for w in env.udb.get_workouts(THREE_WEEKS, end + timedelta(days=60))}
    window = [i for i, w in before.items() if THREE_WEEKS.isoformat() <= w["date"] <= end.isoformat() and w["status"] == "planned" and w["type"] != "race"]
    assert window and r["changed"] == len(window)
    for i in window:
        b, a = before[i], after[i]
        if b["distance_km"] >= 4:
            assert abs(a["distance_km"] - b["distance_km"] * 0.9) <= 0.26 and a["duration_min"] < b["duration_min"]
        assert a["note"].startswith("Eased about 10%")
    by_week = {}
    for i in window:
        if after[i]["type"] in ("tempo", "intervals"):
            by_week.setdefault(after[i]["week_start"], []).append(i)
    assert all(len(v) <= 1 for v in by_week.values())
    assert r["km_removed"] > 0 and r["downgraded"] == sum(1 for i in window if before[i]["type"] in ("tempo", "intervals") and after[i]["type"] == "easy")
    for i, b in before.items():
        if i not in window:
            assert after[i]["distance_km"] == b["distance_km"] and after[i]["type"] == b["type"], b["date"]       # later weeks, race, history untouched
    assert env.udb.kv_get("ease_plan_quiet_until") == (THREE_WEEKS + timedelta(days=14)).isoformat()
    assert "slightly easier" in env.udb.recent_log(1)[0]["message"]
    assert env.svc.ease_suggestion() is None                           # the suggestion doesn't come straight back


# ------------------------------------------------------------------------------------------------------ web -----
def web(tmp_path):
    s = Settings(db_path=str(tmp_path / "x.db"), sync_interval_min=0)
    db = DB(s.db_path)
    clock = {"d": TODAY}
    app = create_app(s, db=db, coach=ReferenceCoach(), strava_factory=lambda u: FakeStrava(), clock=lambda: clock["d"])
    c = TestClient(app)
    signup(c)
    c.post("/setup", data={**SETUP, "baseline": "25", "race_date": str(TODAY + timedelta(weeks=12))})
    c.post("/plan/generate")
    assert wait_job(c)["status"] == "done"
    return c, db, clock


def feed(c, db, clock, until, effort):
    clock["d"] = until
    udb = db.for_user(1)
    acts, n = [], 2000
    for w in udb.get_workouts(end=until - timedelta(days=1)):
        if w["type"] == "race":
            continue
        n += 1
        a = run_activity(n, date.fromisoformat(w["date"]), km=w["distance_km"] * effort, minutes=1)
        a["moving_s"] = int(w["duration_min"] * 60 * effort)
        acts.append(a)
    udb.upsert_activities(acts)
    from coach.service import CoachService
    CoachService(udb, Settings(db_path=db.path), ReferenceCoach(), FakeStrava(), clock=lambda: until).refresh_statuses()


def test_screens_show_scores_and_the_suggestion_card_applies_or_dismisses(tmp_path):
    c, db, clock = web(tmp_path)
    feed(c, db, clock, TODAY + timedelta(days=21), 0.6)
    today = c.get("/").text
    assert "Make the next two weeks a bit easier?" in today and "averaged" in today and 'action="/plan/ease"' in today and 'action="/plan/ease/dismiss"' in today
    assert "% execution" in today or "Execution" in c.get("/plan").text
    plan = c.get("/plan").text
    assert 'title="Execution score"' in plan and "Execution " in plan and "short" in plan
    assert "Execution, last two weeks" in c.get("/progress").text
    run_id = next(w["activity_id"] for w in db.for_user(1).get_workouts() if w["activity_id"])
    run_page = c.get(f"/run/{run_id}").text
    assert ">Execution<" in run_page and "treat it as a guide" in run_page
    r = c.post("/plan/ease", follow_redirects=True)
    assert "Done: the next two weeks are about" in r.text and "Make the next two weeks a bit easier?" not in r.text
    assert "Eased about 10%" in c.get("/plan").text
    again = c.post("/plan/ease", follow_redirects=True)
    assert "no longer needed" in again.text                            # can't be applied twice


def test_not_now_hides_the_card_for_a_week(tmp_path):
    c, db, clock = web(tmp_path)
    feed(c, db, clock, TODAY + timedelta(days=21), 0.6)
    assert "a bit easier" in c.get("/").text
    r = c.post("/plan/ease/dismiss", follow_redirects=True)
    assert "won't suggest it again for a week" in r.text.replace("&#39;", "'") and "a bit easier" not in r.text
    assert db.for_user(1).get_workouts(TODAY + timedelta(days=22))[0].get("note", "") in ("", None)        # dismissing changes nothing in the plan


def test_scores_and_suggestions_are_private_to_each_runner(tmp_path):
    c, db, clock = web(tmp_path)
    feed(c, db, clock, TODAY + timedelta(days=21), 0.6)
    b = TestClient(c.app)
    signup(b, "b@example.com", "B")
    b.post("/setup", data={**SETUP, "baseline": "25"})
    assert "a bit easier" not in b.get("/").text
    assert b.post("/plan/ease", follow_redirects=True).text.count("no longer needed") == 1
    assert db.for_user(2).kv_get("ease_plan_quiet_until") is None


def test_the_suggestion_needs_a_low_average_AND_at_least_half_the_runs_under_70_AND_enough_runs(env, monkeypatch):
    run_the_plan(env, THREE_WEEKS, effort=1.0)
    def with_overview(**kw):
        monkeypatch.setattr(env.svc, "execution_overview", lambda days=14: {"avg": 60, "n": 6, "under": 3, "missed": 0, "too_fast": 0, "short": 0, "main_reason": None, **kw})
        return env.svc.ease_suggestion()
    assert with_overview() is not None
    assert with_overview(under=2) is None                              # average is low but most runs were fine: one bad week isn't a pattern
    assert with_overview(n=3, under=3) is None                         # "regularly" needs at least four scored runs
    assert with_overview(avg=70) is None and with_overview(avg=69) is not None


def test_ease_plan_turns_extra_hard_sessions_in_a_week_into_easy_runs(env):
    ws = [w for w in env.udb.get_workouts(THREE_WEEKS, THREE_WEEKS + timedelta(days=6)) if w["type"] in ("easy", "long") and w["status"] == "planned"][:3]
    for w, kind in zip(ws, ("tempo", "intervals", "tempo")):
        env.udb.update_workout(w["id"], type=kind, title=f"{kind} test")
    env.clock["d"] = THREE_WEEKS
    r = env.svc.ease_plan()
    kinds = [env.udb.get_workout(w["id"])["type"] for w in sorted(ws, key=lambda w: w["date"])]
    assert kinds.count("easy") == 2 and sum(k in ("tempo", "intervals") for k in kinds) == 1 and r["downgraded"] == 2
    assert kinds[0] in ("tempo", "intervals")                          # the first hard session of the week is the one kept
    assert env.udb.get_workout(ws[-1]["id"])["title"] == "Easy run"
