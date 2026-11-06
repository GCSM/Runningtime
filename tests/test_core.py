from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from coach import adherence
from coach.config import Settings
from coach.db import DB
from coach.models import DAYS, DayAvail, Profile
from coach.plancheck import sanitize
from coach.service import CoachService, UserError
from coach.skeleton import build_skeleton, monday
from coach.web import create_app

TODAY = date(2026, 11, 4)  # Wednesday


def avail(days=("tue", "thu", "sat", "sun"), minutes=60):
    return {d: DayAvail(available=d in days, minutes=minutes if d != "sun" else 120) for d in DAYS}


def profile(**kw):
    base = dict(goal_label="Half marathon", goal_km=21.0975, race_date=TODAY + timedelta(weeks=16),
                baseline_km=20, availability=avail(), long_run_day="sun")
    return Profile(**{**base, **kw})


def skel(p, baseline=20, today=TODAY, start=None):
    return build_skeleton(plan_start=start or today, today=today, race_date=p.race_date, goal_km=p.goal_km,
                          baseline_km=baseline, experience=p.experience, weekly_minutes=p.weekly_minutes,
                          long_day_minutes=p.long_day_minutes, easy_pace_s_km=None)


def test_skeleton_shape_and_progression():
    p = profile()
    weeks = skel(p)
    assert weeks[0].week_start == monday(TODAY) and weeks[-1].race_week
    assert weeks[-1].week_start == monday(p.race_date)
    train = [w for w in weeks if w.phase in ("base", "build", "peak")]
    for a, b in zip(train, train[1:]):
        if not b.recovery and not a.recovery:
            assert b.target_km <= a.target_km * 1.09 + 0.5
    assert any(w.recovery for w in weeks)
    assert [w.phase for w in weeks][-3:] == ["taper", "taper", "race"][-3:] or weeks[-1].phase == "race"
    assert weeks[-2].target_km < max(w.target_km for w in train)
    assert all(w.long_run_km <= 22 for w in weeks)


def test_skeleton_respects_time_budget():
    p = profile(availability=avail(days=("tue", "sun"), minutes=30))
    assert max(w.target_km for w in skel(p, baseline=5) if not w.race_week) <= 150 / 6.5 + 1  # 30+120 minutes at 6:30/km


def test_adherence_matching():
    ws = [{"id": 1, "date": "2026-11-03", "distance_km": 8, "duration_min": 50},
          {"id": 2, "date": "2026-11-04", "distance_km": 10, "duration_min": 60},
          {"id": 3, "date": "2026-11-02", "distance_km": 6, "duration_min": 40},
          {"id": 4, "date": "2026-11-05", "distance_km": 6, "duration_min": 40}]
    acts = [{"id": 10, "date": "2026-11-03", "distance_m": 8200, "moving_s": 2900},
            {"id": 11, "date": "2026-11-04", "distance_m": 4000, "moving_s": 1400}]
    m = adherence.match_workouts(ws, acts, TODAY)
    assert m[1]["status"] == "completed" and m[2]["status"] == "partial"
    assert m[3]["status"] == "missed" and m[4]["status"] == "planned"


def test_sanitize_enforces_rules():
    p = profile()
    weeks = skel(p)
    this_wk = weeks[0]
    raw = [
        {"date": "2026-11-05", "type": "tempo", "distance_km": 30, "duration_min": 200, "title": "x"},   # Thu: too long
        {"date": "2026-11-06", "type": "easy", "distance_km": 5, "duration_min": 30},                    # Fri: unavailable
        {"date": "garbage", "type": "easy", "distance_km": 5, "duration_min": 30},
        {"date": "2026-11-03", "type": "easy", "distance_km": 5, "duration_min": 30},                    # past
    ]
    clean, notes = sanitize(raw, profile=p, weeks=weeks, today=TODAY, blocked_dates=set(), done_km_by_week={}, pace_s_km=390)
    dates = [w["date"] for w in clean]
    assert "2026-11-06" not in dates and "2026-11-03" not in dates
    thu = next(w for w in clean if w["date"] == "2026-11-05")
    assert thu["duration_min"] <= 66 and thu["distance_km"] <= this_wk.target_km * 1.1
    assert clean[-1]["type"] == "race" and clean[-1]["date"] == p.race_date.isoformat()


# ---- full flow with fakes -----------------------------------------------------
class FakeCoach:
    def __init__(self):
        self.calls = []

    def plan(self, ctx, *, mode, feedback=None):
        self.calls.append((mode, ctx, feedback))
        out = []
        for wk in ctx["skeleton"]:
            ws = date.fromisoformat(wk["week_start"])
            for off, typ, share in ((1, "easy", .25), (3, "tempo", .25), (5, "easy", .2), (6, "long", .3)):
                d = ws + timedelta(days=off)
                if d.isoformat() >= ctx["earliest_workout_date"] and not wk["race_week"]:
                    out.append({"date": d.isoformat(), "type": typ, "title": typ, "description": "", "pace_guidance": "",
                                "distance_km": round(wk["target_km"] * share, 1), "duration_min": 40})
        return {"summary": f"{mode} ok", "workouts": out}


class FakeStrava:
    connected = True

    def __init__(self):
        self.runs = []

    def athlete_name(self):
        return "Test"

    def fetch_runs(self, after_ts):
        return self.runs


@pytest.fixture
def svc(tmp_path):
    s = Settings(db_path=str(tmp_path / "t.db"), sync_interval_min=0)
    clock = {"d": TODAY}
    service = CoachService(DB(s.db_path), s, FakeCoach(), FakeStrava(), clock=lambda: clock["d"])
    service.clock = clock
    return service


def test_generate_then_adapt_to_strava(svc):
    with pytest.raises(UserError):
        svc.generate_plan()
    svc.db.save_profile(profile())
    svc.generate_plan()
    plan = svc.db.get_workouts()
    assert plan[-1]["type"] == "race" and len(plan) > 40
    first_target = svc.db.get_weeks()[0]["target_km"]

    # Athlete skips the Thursday tempo and only runs 3km on Saturday -> next review sees low adherence.
    svc.clock["d"] = TODAY + timedelta(days=7)
    svc.strava.runs = [
        {"id": 1, "start_local": "2026-11-03T07:00:00", "date": "2026-11-03", "name": "Run", "distance_m": 6000, "moving_s": 2000, "avg_hr": 150, "elevation_m": 0, "sport": "Run"},
        {"id": 2, "start_local": "2026-11-07T07:00:00", "date": "2026-11-07", "name": "Run", "distance_m": 3000, "moving_s": 1100, "avg_hr": 160, "elevation_m": 0, "sport": "Run"},
    ]
    summary = svc.review("calf felt tight")
    mode, ctx, _ = svc.coach.calls[-1]
    assert mode == "adjust" and ctx["athlete_note"] == "calf felt tight"
    review = ctx["last_4_weeks_review"]
    assert any(w["actual_km_all_runs"] == 9.0 for w in review)
    assert any(r["status"] == "missed" for w in review for r in w["workouts"])
    # Skeleton is re-derived from what was actually run, so targets drop below the original plan.
    new_first = svc.db.get_weeks()
    assert next(w for w in new_first if w["week_start"] == monday(svc.clock["d"]).isoformat())["target_km"] < first_target + 5
    assert svc.db.recent_log(1)[0]["message"].startswith("You said: calf")
    # History is preserved; future replaced
    assert any(w["status"] == "missed" for w in svc.db.get_workouts(end=svc.clock["d"]))


def test_web_flow(svc):
    c = TestClient(create_app(svc.settings, svc))
    assert c.get("/", follow_redirects=False).status_code == 303
    form = {"name": "G", "units": "km", "goal_preset": "10K", "race_date": str(TODAY + timedelta(weeks=12)), "baseline": "15",
            "experience": "intermediate", "long_run_day": "sun", "av_tue": "on", "min_tue": "45", "av_thu": "on", "min_thu": "45",
            "av_sun": "on", "min_sun": "90", "goal_time": "50:00"}
    assert c.post("/setup", data=form, follow_redirects=False).status_code == 303
    assert svc.db.get_profile().goal_time_s == 3000
    r = c.post("/plan/generate", follow_redirects=True)
    assert "Plan created" in r.text and "Race day" not in "" and "This week" in r.text
    assert "Full plan" in c.get("/plan").text
    assert c.post("/setup", data={**form, "av_sun": ""}, follow_redirects=True).text.count("long-run day") == 1
    assert c.post("/review", data={"note": "good"}, follow_redirects=True).status_code == 200
    assert c.post("/plan/generate", headers={"origin": "http://evil.example"}).status_code == 403


def test_password(svc):
    s = Settings(db_path=svc.settings.db_path, app_password="pw", sync_interval_min=0)
    c = TestClient(create_app(s, svc))
    assert c.get("/").status_code == 401 and c.get("/healthz").status_code == 200
    assert c.get("/", auth=("me", "pw"), follow_redirects=False).status_code == 303


def test_marathon_race_week_has_room_for_the_race():
    p = profile(goal_km=42.195, goal_label="Marathon", race_date=TODAY + timedelta(weeks=20))
    weeks = skel(p, baseline=40)
    assert weeks[-1].race_week and weeks[-1].target_km >= 42.195 + 6
