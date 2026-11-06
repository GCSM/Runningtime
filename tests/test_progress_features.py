"""Fitness estimate/prediction, heatmap, milestones (celebrated once), race week / race day / after the race."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from coach import badges as B
from coach import fitness as F
from coach import heatmap as H
from coach import knowledge as K
from coach.config import Settings
from coach.db import DB
from coach.errors import UserError
from coach.skeleton import monday
from coach.web import create_app
from test_actions import Env, TODAY
from test_core import FakeStrava, signup, wait_job
from test_scenarios import ReferenceCoach, SETUP, prof, run_activity


def act(i, d, km=5.0, pace=360, **kw):
    a = {"id": i, "date": d.isoformat(), "start_local": d.isoformat() + "T07:00:00", "distance_m": km * 1000, "moving_s": int(km * pace), "avg_hr": 150.0,
         "elevation_m": 5.0, "name": "Run", "sport": "Run"}
    a.update(kw)
    return a


# ====================================================================================================== fitness ===
def test_only_runs_that_can_tell_us_something_count():
    assert F.run_vdot(act(1, TODAY, km=5, pace=330)) is not None
    assert F.run_vdot(act(1, TODAY, km=2.9, pace=300)) is None                    # too short
    assert F.run_vdot(act(1, TODAY, km=3.0, pace=200)) is None                    # under 12 minutes
    assert F.run_vdot(act(1, TODAY, km=31, pace=330)) is None                     # too long to be a clean effort
    assert F.run_vdot(act(1, TODAY, km=5, pace=100)) is None                      # 1:40/km: GPS glitch, not a runner
    assert F.run_vdot(act(1, TODAY, km=5, pace=1500)) is None                     # a walk: below any plausible fitness


def test_estimate_uses_the_best_recent_effort_and_ignores_old_ones():
    acts = [act(1, TODAY - timedelta(weeks=10), pace=270),                         # a great run, but 10 weeks ago
            act(2, TODAY - timedelta(weeks=3), pace=360), act(3, TODAY - timedelta(weeks=1), pace=340), act(4, TODAY - timedelta(days=2), pace=380)]
    e = F.estimate(acts, TODAY)
    assert e["activity_id"] == 3 and e["km"] == 5.0
    assert F.estimate([], TODAY) is None and F.estimate([acts[0]], TODAY) is None


def test_vdot_trend_rises_as_the_runner_improves_and_has_gaps_when_there_is_no_data():
    acts = [act(i, TODAY - timedelta(weeks=w), pace=p) for i, (w, p) in enumerate([(10, 380), (7, 365), (4, 350), (1, 335)])]
    t = F.trend(acts, TODAY, 12)
    vals = [x["vdot"] for x in t if x["vdot"] is not None]
    assert len(t) == 12 and vals == sorted(vals) and vals[-1] > vals[0] + 3
    assert t[0]["vdot"] is None                                                    # before the first run there is no estimate
    assert all(x["week_start"] == monday(date.fromisoformat(x["week_start"])).isoformat() for x in t)


def test_predictions_agree_with_the_knowledge_module_and_get_slower_with_distance():
    v = F.estimate([act(1, TODAY - timedelta(days=3), pace=330)], TODAY)["vdot"]
    p = F.predictions(v)
    assert list(p) == ["5K", "10K", "Half marathon", "Marathon"]
    assert p["5K"] == F.fmt_time(K.race_time_s(v, 5000))
    secs = [sum(int(x) * m for x, m in zip(reversed(t.split(":")), (1, 60, 3600))) for t in p.values()]
    assert secs == sorted(secs) and secs[0] < 5 * 330 * 1.0 + 5 * 60                # a 5K prediction is faster than the training pace implies slower
    assert F.fmt_time(65) == "1:05" and F.fmt_time(3725) == "1:02:05"


@pytest.mark.parametrize("goal_s,tone", [(1800, "ahead"), (1709, "edge"), (1660, "stretch"), (1300, "ambitious")])
def test_goal_assessment_is_honest_about_the_gap(goal_s, tone):
    v = K.vdot(5000, 1709)                                                          # fitness for ~28:29
    g = F.goal_assessment(v, 5.0, goal_s)
    assert g["tone"] == tone and g["predicted"] == "28:29"
    assert (g["gap_pct"] >= 0) == (goal_s >= g["predicted_s"])
    assert F.goal_assessment(v, 5.0, None) is None


def test_sparkline_fits_its_box_and_needs_two_points():
    assert F.sparkline([{"week_start": "x", "vdot": None}, {"week_start": "y", "vdot": 40.0}]) is None
    s = F.sparkline([{"vdot": 30.0}, {"vdot": None}, {"vdot": 33.0}, {"vdot": 36.0}], 240, 56, 6)
    pts = [tuple(map(float, c.replace("M", "").split())) for c in s["d"].split(" L")]
    assert len(pts) == 3 and all(6 <= x <= 234 and 6 <= y <= 50 for x, y in pts)
    assert pts[0][1] > pts[-1][1]                                                   # rising fitness goes UP the chart
    assert s["last"] == pts[-1]


def test_view_bundles_everything_and_survives_no_data():
    empty = F.view([], TODAY, 5.0, 1320)
    assert empty["estimate"] is None and empty["predictions"] is None and empty["goal"] is None and empty["spark"] is None
    acts = [act(i, TODAY - timedelta(weeks=w), pace=p) for i, (w, p) in enumerate([(8, 380), (1, 335)])]
    v = F.view(acts, TODAY, 5.0, 1320)
    assert v["estimate"] and v["predictions"] and v["goal"]["goal"] == "22:00" and v["delta"] is not None and v["delta"] > 0


# ===================================================================================================== heatmap ===
def test_heatmap_grid_positions_levels_and_windows():
    acts = [act(1, TODAY - timedelta(days=1), km=10), act(2, TODAY - timedelta(days=3), km=5), act(3, TODAY - timedelta(days=3), km=3),   # two runs one day
            act(4, TODAY - timedelta(days=5), km=2), act(5, TODAY - timedelta(days=9), km=12), act(6, TODAY - timedelta(days=11), km=6),
            act(7, TODAY - timedelta(weeks=40), km=20)]                                                                                    # outside the window
    h = H.build(acts, TODAY, weeks=20)
    assert len(h["cells"]) == 140 and h["weeks"] == 20 and h["runs"] == 6 and h["active_days"] == 5
    by = {c["date"]: c for c in h["cells"]}
    two = by[(TODAY - timedelta(days=3)).isoformat()]
    assert two["km"] == 8.0 and two["level"] >= 1 and two["run_id"] == 3
    assert by[(TODAY - timedelta(days=9)).isoformat()]["level"] == 4 and by[(TODAY - timedelta(days=5)).isoformat()]["level"] == 1
    assert by[(TODAY - timedelta(days=2)).isoformat()]["level"] == 0 and by[(TODAY - timedelta(days=2)).isoformat()]["run_id"] is None
    assert by[(TODAY + timedelta(days=1)).isoformat()]["future"] is True and by[TODAY.isoformat()]["future"] is False
    last_col = [c for c in h["cells"] if c["col"] == 20]
    assert [c["row"] for c in last_col] == list(range(1, 8)) and last_col[0]["date"] == monday(TODAY).isoformat()      # Monday on top
    assert all(1 <= c["col"] <= 20 and 1 <= c["row"] <= 7 for c in h["cells"])
    assert h["months"][0]["col"] == 1 and len({m["label"] for m in h["months"]}) == len(h["months"])


def test_heatmap_with_no_runs_is_all_empty():
    h = H.build([], TODAY)
    assert h["runs"] == 0 and h["active_days"] == 0 and all(c["level"] == 0 and c["run_id"] is None for c in h["cells"])


# ==================================================================================================== milestones ===
def ctx(acts=(), workouts=(), **kw):
    base = dict(acts=list(acts), workouts=list(workouts), today=TODAY, goal_km=10.0, goal_time_s=3000, plan_start=None, race_date=None)
    base.update(kw)
    return B.Ctx(**base)


def earned(c):
    return {b["id"]: b["earned"] for b in B.evaluate(c) if b["earned"]}


def wk(i, d, status, typ="easy", week=None, activity_id=None):
    return {"id": i, "date": d.isoformat(), "week_start": (week or monday(d)).isoformat(), "type": typ, "status": status, "activity_id": activity_id}


def test_catalogue_is_well_formed():
    ids = [c[0] for c in B.CATALOG]
    assert len(ids) == len(set(ids)) == 21
    assert all(c[1] and c[2] and c[3] and callable(c[4]) for c in B.CATALOG)
    assert earned(ctx()) == {}                                                       # a brand-new athlete has nothing


def test_run_count_distance_and_total_km_badges_carry_the_date_they_were_earned():
    acts = [act(i, TODAY - timedelta(days=40 - i), km=3 + i * 0.5) for i in range(1, 31)]
    e = earned(ctx(acts))
    assert e["first_run"] == (TODAY - timedelta(days=39)).isoformat() and e["runs_10"] == (TODAY - timedelta(days=30)).isoformat()
    assert e["runs_25"] == (TODAY - timedelta(days=15)).isoformat() and "runs_50" not in e
    assert e["first_5k"] == (TODAY - timedelta(days=36)).isoformat()                 # 3 + 4*0.5 = 5.0 km on the 4th run
    assert "first_10k" in e and "first_half" not in e                                 # longest is 18 km
    assert "km_100" in e and "km_250" in e and "km_500" not in e


def test_distance_badges_have_a_small_gps_tolerance():
    assert "first_5k" in earned(ctx([act(1, TODAY, km=4.98)])) and "first_5k" not in earned(ctx([act(1, TODAY, km=4.9)]))


def test_streaks_ignore_skips_and_reset_on_a_miss():
    days = [TODAY - timedelta(days=20 - i) for i in range(12)]
    seq = ["completed"] * 3 + ["skipped"] + ["completed"] * 2 + ["missed"] + ["completed"] * 5
    ws = [wk(i, d, s) for i, (d, s) in enumerate(zip(days, seq))]
    e = earned(ctx(workouts=ws))
    assert e["streak_5"] == days[5].isoformat()                                       # 3 + (skip ignored) + 2 = 5th completed on day 5
    assert "streak_10" not in e                                                       # the miss reset the count; only 5 since
    assert earned(ctx(workouts=[wk(i, d, "completed") for i, d in enumerate(days[:11])]))["streak_10"] == days[9].isoformat()


def test_perfect_week_needs_three_runs_all_completed():
    mon = monday(TODAY - timedelta(days=14))
    good = [wk(1, mon, "completed"), wk(2, mon + timedelta(days=2), "completed"), wk(3, mon + timedelta(days=5), "completed", "long")]
    assert earned(ctx(workouts=good))["perfect_week"] == (mon + timedelta(days=5)).isoformat()
    assert "perfect_week" not in earned(ctx(workouts=good[:2]))                       # two runs aren't a week
    assert "perfect_week" not in earned(ctx(workouts=[*good[:2], wk(3, mon + timedelta(days=5), "partial")]))
    assert "perfect_week" not in earned(ctx(workouts=[*good[:2], wk(3, mon + timedelta(days=5), "skipped")]))


def test_first_hard_session_and_halfway():
    ws = [wk(1, TODAY - timedelta(days=9), "completed", "easy"), wk(2, TODAY - timedelta(days=5), "planned", "tempo"), wk(3, TODAY - timedelta(days=3), "partial", "intervals")]
    assert earned(ctx(workouts=ws))["first_hard"] == (TODAY - timedelta(days=3)).isoformat()
    start, race = TODAY - timedelta(weeks=5), TODAY + timedelta(weeks=5)
    assert earned(ctx(plan_start=start, race_date=race))["halfway"] == TODAY.isoformat()
    assert "halfway" not in earned(ctx(plan_start=start, race_date=race + timedelta(weeks=4)))     # midpoint still ahead
    assert "halfway" not in earned(ctx(plan_start=TODAY - timedelta(weeks=3), race_date=TODAY + timedelta(weeks=3)))   # plans under 8 weeks have no midpoint badge


def test_race_badges_need_the_distance_and_for_the_goal_time_the_time():
    race_day = TODAY
    ws = [wk(1, race_day, "completed", "race", activity_id=77)]
    ok = [act(77, race_day, km=10.0, pace=290)]                                       # 48:20, under the 50:00 goal
    slow = [act(77, race_day, km=10.0, pace=310)]
    short = [act(77, race_day, km=9.0, pace=280)]
    assert {"race_finisher", "goal_time"} <= set(earned(ctx(ok, ws)))
    e = earned(ctx(slow, ws))
    assert "race_finisher" in e and "goal_time" not in e
    assert "race_finisher" not in earned(ctx(short, ws)) and "goal_time" not in earned(ctx(short, ws))
    assert "goal_time" not in earned(ctx(ok, ws, goal_time_s=None))
    assert not {"race_finisher", "goal_time"} & set(earned(ctx(ok, [wk(1, race_day, "planned", "race", activity_id=77)])))


# ================================================================================ service: celebrate once ===
def test_existing_history_is_not_celebrated_on_first_look_but_new_badges_are_once(tmp_path):
    env = Env(tmp_path)
    env.udb.upsert_activities([act(100 + i, TODAY - timedelta(days=30 - i), km=3.5) for i in range(12)])      # an athlete with history
    first = env.svc.badges()
    assert first["count"] == 2 and first["new"] == []                                  # history is history: no confetti storm
    assert env.svc.badges()["new"] == []
    env.udb.upsert_activities([act(200 + i, TODAY - timedelta(days=12 - i), km=3.5) for i in range(15)])      # runs 13..27 -> "25 runs"
    again = env.svc.badges()
    assert [b["id"] for b in again["new"]] == ["runs_25"]
    assert [b["id"] for b in env.svc.badges()["new"]] == ["runs_25"]                   # stays "new" until acknowledged (survives a reload)
    env.svc.mark_badges_seen()
    assert env.svc.badges()["new"] == []


def test_a_brand_new_athletes_first_run_is_celebrated(tmp_path):
    env = Env(tmp_path, plan=False)
    assert env.svc.badges() == {"earned": [], "locked": env.svc.badges()["locked"], "new": [], "count": 0, "total": 21}
    env.udb.upsert_activities([act(1, TODAY, km=3.5)])
    assert [b["id"] for b in env.svc.badges()["new"]] == ["first_run"]


def test_badges_are_per_user(tmp_path):
    env = Env(tmp_path)
    env.udb.upsert_activities([act(1, TODAY - timedelta(days=1))])
    other = env.db.create_user("b@example.com", "B", "x")
    from coach.service import CoachService
    svc2 = CoachService(env.db.for_user(other), Settings(db_path=env.db.path), env.coach, FakeStrava(), clock=lambda: TODAY)
    assert svc2.badges()["count"] == 0 and env.svc.badges()["count"] >= 1


# ============================================================================== race week / race day / after ===
def at(env, days_before):
    env.clock["d"] = env.udb.get_profile().race_date - timedelta(days=days_before)


def test_race_modes_follow_the_calendar(tmp_path):
    env = Env(tmp_path)
    modes = {}
    for d in (30, 8, 7, 3, 0, -1, -20):
        at(env, d)
        modes[d] = env.svc.race_state()["mode"]
    assert modes == {30: "normal", 8: "normal", 7: "week", 3: "week", 0: "day", -1: "after", -20: "after"}
    at(env, 30)
    assert "checklist" not in env.svc.race_state()


def test_checklist_size_follows_distance_and_state_persists_and_resets_per_race(tmp_path):
    sizes = {5.0: 6, 10.0: 6, 21.0975: 9, 42.195: 10}
    for km, n in sizes.items():
        assert len(K.race_checklist(km)) == n, km
    env = Env(tmp_path)
    at(env, 3)
    items = env.svc.race_state()["checklist"]
    assert all(not c["done"] for c in items)
    env.svc.toggle_checklist("kit", True)
    env.svc.toggle_checklist("travel", True)
    env.svc.toggle_checklist("kit", False)
    done = {c["id"] for c in env.svc.race_state()["checklist"] if c["done"]}
    assert done == {"travel"}
    with pytest.raises(UserError, match="wasn't found"):
        env.svc.toggle_checklist("nope", True)
    with pytest.raises(UserError, match="wasn't found"):
        env.svc.toggle_checklist("fuel", True)                                          # not on a 10K list
    p = env.udb.get_profile()
    env.udb.save_profile(p.model_copy(update={"race_date": p.race_date + timedelta(weeks=20)}))  # a new race: fresh list
    at(env, 3)
    assert all(not c["done"] for c in env.svc.race_state()["checklist"])


def test_pace_plan_checkpoints_and_units():
    half = K.pace_plan(21.0975, 6300, "km")
    assert [c["label"] for c in half] == ["5 km", "10 km", "15 km", "20 km", "Finish"] and half[-1]["elapsed_s"] == 6300
    assert half[0]["elapsed_s"] == round(6300 / 21.0975 * 5) and [c["elapsed_s"] for c in half] == sorted(c["elapsed_s"] for c in half)
    assert [c["label"] for c in K.pace_plan(5.0, 1320, "km")] == ["Finish"]              # a 5K needs no checkpoints
    mi = K.pace_plan(42.195, 14400, "mi")
    assert mi[0]["label"] == "5 mi" and mi[0]["elapsed_s"] == round(14400 / 42.195 * 5 * K.KM_PER_MI) and mi[-1]["label"] == "Finish"
    assert [c["label"] for c in mi] == ["5 mi", "10 mi", "15 mi", "20 mi", "25 mi", "Finish"]


def test_race_week_state_includes_the_pace_plan_only_with_a_goal_time(tmp_path):
    env = Env(tmp_path)
    at(env, 2)
    assert env.svc.race_state()["pace_plan"] is None
    p = env.udb.get_profile()
    env.udb.save_profile(p.model_copy(update={"goal_time_s": 3000}))
    st = env.svc.race_state()
    assert st["pace_plan"][-1]["elapsed_s"] == 3000 and st["pace_s_km"] == 300


def test_after_the_race_the_result_and_goal_outcome_are_reported(tmp_path):
    env = Env(tmp_path)
    p = env.udb.get_profile()
    env.udb.save_profile(p.model_copy(update={"goal_time_s": 3000}))
    race = next(w for w in env.udb.get_workouts() if w["type"] == "race")
    at(env, -2)
    assert env.svc.race_state()["result"] is None                                       # no run synced yet
    env.udb.upsert_activities([act(900, p.race_date, km=10.0, pace=288)])
    env.udb.update_workout(race["id"], status="completed", activity_id=900)
    r = env.svc.race_state()["result"]
    assert r["time_s"] == 2880 and r["hit_goal"] is True and r["activity_id"] == 900
    env.udb.upsert_activities([act(900, p.race_date, km=10.0, pace=312)])
    assert env.svc.race_state()["result"]["hit_goal"] is False
    env.udb.upsert_activities([act(900, p.race_date, km=9.0, pace=270)])               # fast, but short of the distance
    assert env.svc.race_state()["result"]["hit_goal"] is False


# ====================================================================================================== web ===
def web(tmp_path, clock):
    strava = FakeStrava()
    s = Settings(db_path=str(tmp_path / "w.db"), sync_interval_min=0)
    db = DB(s.db_path)
    app = create_app(s, db=db, coach=ReferenceCoach(), strava_factory=lambda u: strava, clock=lambda: clock["d"])
    c = TestClient(app)
    signup(c)
    c.post("/setup", data={**SETUP, "baseline": "25", "goal_time": "20:00"})
    c.post("/plan/generate")
    assert wait_job(c)["status"] == "done"
    return c, db


def test_progress_page_shows_fitness_heatmap_and_milestones(tmp_path):
    clock = {"d": TODAY}
    c, db = web(tmp_path, clock)
    db.for_user(1).upsert_activities([act(10 + i, TODAY - timedelta(days=i * 2 + 1), km=5, pace=340 - i) for i in range(8)])
    page = c.get("/progress").text
    assert ">Fitness<" in page and "Fitness score (VDOT)" in page and "Goal 5K in 20:00" in page and "Half marathon" in page
    assert page.count('class="hc ') >= 140 and 'href="/run/10"' in page and "Consistency" in page
    assert "Milestones" in page and "First run logged" in page and "badge locked" in page
    assert "not a race" in page                                                          # the honesty note about easy runs


def test_progress_page_without_runs_degrades_gracefully(tmp_path):
    c, _ = web(tmp_path, {"d": TODAY})
    page = c.get("/progress").text
    assert "Once you've logged a run of 3 km or more" in page and "0 runs on 0 days" in page


def test_today_celebrates_a_new_badge_once(tmp_path):
    clock = {"d": TODAY}
    c, db = web(tmp_path, clock)
    assert 'id="celebrate"' not in c.get("/").text
    db.for_user(1).upsert_activities([act(1, TODAY - timedelta(days=1), km=5.2)])
    page = c.get("/").text
    assert 'id="celebrate"' in page and "First run logged" in page and "First 5K run" in page
    assert c.post("/api/badges/seen", headers={"X-Requested-With": "fetch"}).json() == {"ok": True}
    assert 'id="celebrate"' not in c.get("/").text
    assert TestClient(c.app).post("/api/badges/seen").status_code in (303, 401)          # an unauthenticated client can't mark anything seen


def test_today_in_race_week_shows_checklist_and_pace_plan_and_ticking_persists(tmp_path):
    clock = {"d": TODAY}
    c, db = web(tmp_path, clock)
    race = db.for_user(1).get_profile().race_date
    clock["d"] = race - timedelta(days=4)
    page = c.get("/").text
    assert 'id="race-week"' in page and "Race week" in page and "Nothing new on race day" in page and "Pace plan" in page
    assert page.count('class="check ') == 6
    c.post("/race-checklist", data={"item": "kit", "checked": "1"})
    assert 'class="check done"' in c.get("/").text
    assert c.post("/race-checklist", data={"item": "bogus", "checked": "1"}, follow_redirects=True).status_code == 200
    clock["d"] = race
    assert "Race day" in c.get("/").text and 'class="check done"' in c.get("/").text
    clock["d"] = race - timedelta(days=20)
    assert 'id="race-week"' not in c.get("/").text


def test_today_after_the_race_shows_the_result_and_a_next_goal_prompt(tmp_path):
    clock = {"d": TODAY}
    c, db = web(tmp_path, clock)
    p = db.for_user(1).get_profile()
    race = next(w for w in db.for_user(1).get_workouts() if w["type"] == "race")
    clock["d"] = p.race_date + timedelta(days=2)
    page = c.get("/").text
    assert "Race complete" in page and "Set your next goal" in page and "days to 5K" not in page
    assert "adjust plan" not in page and "Check in with your coach" not in page          # nothing left to adjust once the race is over
    clock["d"] = p.race_date - timedelta(days=20)
    assert "Check in with your coach" in c.get("/").text                                 # …but it's there during the build
    clock["d"] = p.race_date + timedelta(days=2)
    db.for_user(1).upsert_activities([act(900, p.race_date, km=5.0, pace=230)])
    db.for_user(1).update_workout(race["id"], status="completed", activity_id=900)
    page = c.get("/").text
    assert "19:10" in page and "You hit your goal time" in page and 'href="/run/900"' in page
    assert 'href="/setup"' in page
