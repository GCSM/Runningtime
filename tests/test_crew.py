"""Crew: opt-in only, minimal shared fields, one cheer per pair per week, private by default."""
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from coach import crew as C
from coach.config import Settings
from coach.db import DB
from coach.web import create_app
from test_chat import FakeChat, TODAY
from test_core import FakeStrava, signup, wait_job
from test_scenarios import ReferenceCoach, SETUP, run_activity


def build(tmp_path, n=3):
    s = Settings(db_path=str(tmp_path / "c.db"), sync_interval_min=0)
    db = DB(s.db_path)
    app = create_app(s, db=db, coach=ReferenceCoach(), strava_factory=lambda u: FakeStrava(), clock=lambda: TODAY)
    clients = []
    for i in range(n):
        c = TestClient(app)
        signup(c, f"u{i}@example.com", ["Ann Lee", "Bo Chan", "Cy Dunn"][i])
        c.post("/setup", data={**SETUP, "baseline": "25"})
        clients.append(c)
    return clients, db


def test_nothing_is_visible_without_opting_in(tmp_path):
    (a, b, _), db = build(tmp_path)
    db.for_user(2).upsert_activities([run_activity(1, TODAY, km=9)])
    a.post("/crew/join")
    page = a.get("/crew").text
    assert "Ann" in page and not re.search(r"\bBo\b", page)                           # b has not joined: invisible
    assert "Join the crew" in b.get("/crew").text and "Ann" not in b.get("/crew").text      # b can't peek either
    assert C.crew(db, 2, TODAY) == []


def test_members_see_only_the_agreed_fields_ranked_by_distance(tmp_path):
    (a, b, c), db = build(tmp_path)
    db.for_user(1).upsert_activities([run_activity(1, TODAY, km=5)])
    db.for_user(2).upsert_activities([run_activity(2, TODAY, km=12)])
    for x in (a, b):
        x.post("/crew/join")
    people = C.crew(db, 1, TODAY)
    assert [p["first_name"] for p in people] == ["Bo", "Ann"] and people[0]["week_km"] == 12.0
    assert set(people[0]) == {"user_id", "first_name", "units", "week_km", "runs", "streak", "goal", "days_to_race", "me", "kudos", "given"}
    page = a.get("/crew").text
    assert "Chan" not in page and "u1@example.com" not in page and not re.search(r"\bCy\b", page)


def test_old_runs_dont_count_toward_this_week(tmp_path):
    (a, *_), db = build(tmp_path, 1)
    db.for_user(1).upsert_activities([run_activity(1, TODAY - timedelta(days=14), km=20)])
    a.post("/crew/join")
    assert C.crew(db, 1, TODAY)[0]["week_km"] == 0 and C.crew(db, 1, TODAY)[0]["runs"] == 0


def test_kudos_rules(tmp_path):
    (a, b, c), db = build(tmp_path)
    a.post("/crew/join"); b.post("/crew/join")
    r = a.post("/crew/kudos/2", data={"emoji": "🔥"}, follow_redirects=True)
    assert "🔥" in b.get("/crew").text
    a.post("/crew/kudos/2", data={"emoji": "💪"})                          # one per pair per week: replaces
    assert next(p for p in C.crew(db, 2, TODAY) if p["me"])["kudos"] == ["💪"]
    assert "Pick one" in a.post("/crew/kudos/2", data={"emoji": "💩"}, follow_redirects=True).text
    assert "can&#39;t cheer for yourself" in a.post("/crew/kudos/1", data={"emoji": "🔥"}, follow_redirects=True).text
    assert "isn&#39;t in your crew" in a.post("/crew/kudos/3", data={"emoji": "🔥"}, follow_redirects=True).text
    assert "isn&#39;t in your crew" in c.post("/crew/kudos/1", data={"emoji": "🔥"}, follow_redirects=True).text   # non-member can't send
    a.post("/crew/leave")
    assert C.crew(db, 2, TODAY) and all(p["first_name"] != "Ann" for p in C.crew(db, 2, TODAY))
    assert len(db.kudos_for_week(C.monday(TODAY).isoformat())) == 1                 # leaving doesn't erase what others sent


def test_kudos_resets_next_week_and_deleted_accounts_vanish(tmp_path):
    (a, b, _), db = build(tmp_path)
    a.post("/crew/join"); b.post("/crew/join")
    a.post("/crew/kudos/2", data={"emoji": "🔥"})
    assert not any(p["kudos"] for p in C.crew(db, 2, TODAY + timedelta(days=7)))
    b.post("/account/delete", data={"confirm": "u1@example.com"})
    assert [p["first_name"] for p in C.crew(db, 1, TODAY)] == ["Ann"] and db.kudos_for_week(C.monday(TODAY).isoformat()) == []


def test_login_required_and_me_links_to_crew(tmp_path):
    (a, *_), db = build(tmp_path, 1)
    anon = TestClient(a.app)
    assert anon.get("/crew", follow_redirects=False).status_code in (303, 401)
    assert 'href="/crew"' in a.get("/me").text


def test_theme_toggle_markup_and_css_overrides(tmp_path):
    (a, *_), _ = build(tmp_path, 1)
    me, base = a.get("/me").text, a.get("/crew").text
    assert 'id="theme-seg"' in me and all(f'data-t="{t}"' in me for t in ("auto", "light", "dark"))
    assert 'localStorage.getItem("theme")' in base                                       # applied before first paint (no flash)
    css = a.get("/static/app.css").text
    assert ':root[data-theme="dark"]{' in css and ':root:not([data-theme="light"])' in css


def test_setup_wizard_only_for_new_users_and_form_still_posts_without_js(tmp_path):
    (a, *_), db = build(tmp_path, 1)
    anon = TestClient(a.app)
    signup(anon, "new@example.com", "New")
    first = anon.get("/setup").text
    assert 'data-wizard="1"' in first and "Build my plan" in first and 'name="race_date"' in first      # fields all present for no-JS
    again = a.get("/setup").text                                                                          # already has a profile
    assert 'data-wizard' not in again and ">Save<" in again


def test_service_worker_is_served_from_root_and_never_caches_posts_or_api(tmp_path):
    (a, *_), _ = build(tmp_path, 1)
    anon = TestClient(a.app)
    r = anon.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"] and r.headers["service-worker-allowed"] == "/"
    assert 'r.method !== "GET"' in r.text and '"/api"' in r.text and "isSecureContext" in a.get("/").text
    assert "caches.delete" in a.get("/me").text                                          # cached pages are wiped at sign-out


def test_target_time_uses_separate_number_boxes_and_the_old_field_still_works(tmp_path):
    from coach.web import goal_seconds
    assert goal_seconds({"goal_h": "1", "goal_m": "45", "goal_s": "30"}) == 6330
    assert goal_seconds({"goal_h": "", "goal_m": "50", "goal_s": ""}) == 3000            # blanks count as zero: a 5K in 50 minutes
    assert goal_seconds({"goal_h": "", "goal_m": "", "goal_s": ""}) is None
    assert goal_seconds({"goal_time": "50:00"}) == 3000
    with pytest.raises(ValueError):
        goal_seconds({"goal_m": "75"})
    (a, *_), db = build(tmp_path, 1)
    page = a.get("/setup").text
    assert 'name="goal_m"' in page and 'inputmode="numeric"' in page and 'name="goal_time"' not in page
    a.post("/setup", data={**SETUP, "baseline": "25", "goal_h": "0", "goal_m": "49", "goal_s": "30"})
    assert db.for_user(1).get_profile().goal_time_s == 2970
    assert 'value="49"' in a.get("/setup").text and 'value="30"' in a.get("/setup").text


def test_setup_warns_immediately_and_the_dashboard_keeps_the_warning(tmp_path):
    (a, *_), db = build(tmp_path, 1)
    marathon = {**SETUP, "baseline": "25", "goal_preset": "Marathon", "race_date": str(TODAY + timedelta(weeks=20)),
                "min_tue": "40", "min_thu": "40", "min_sat": "40", "min_sun": "60"}
    r = a.post("/setup", data=marathon, follow_redirects=True)
    assert "heads-up" in r.text and "long-run day has 60 minutes" in r.text
    assert "This plan won’t fully prepare you" in a.get("/").text
    ok = a.post("/setup", data={**marathon, "av_mon": "on", "min_mon": "75", "min_sun": "210", "min_tue": "75", "min_thu": "75", "min_sat": "75", "baseline": "40", "race_date": str(TODAY + timedelta(weeks=28))}, follow_redirects=True)
    assert "heads-up" not in ok.text and "won’t fully prepare" not in a.get("/").text


def test_run_length_is_a_choice_of_categories_not_free_numbers(tmp_path):
    (a, *_), db = build(tmp_path, 1)
    page = a.get("/setup").text
    assert 'type="radio" name="min_sun"' in page and 'name="min_sun" inputmode' not in page
    for nm in ("Short", "Medium", "Long", "Extra long", "Open-ended"):
        assert f"<span>{nm}<small>" in page
    a.post("/setup", data={**SETUP, "baseline": "25", "min_sun": "150", "min_tue": "40"})
    p = db.for_user(1).get_profile()
    assert p.availability["sun"].minutes == 150 and p.availability["tue"].minutes == 40
    page = a.get("/setup").text
    assert 'name="min_sun" value="150" checked' in page and 'name="min_tue" value="40" checked' in page
    db.for_user(1).save_profile(p.model_copy(update={"availability": {**p.availability, "thu": p.availability["thu"].model_copy(update={"minutes": 75})}}))
    assert 'name="min_thu" value="60" checked' in a.get("/setup").text            # an old free-form 75 snaps to the nearest category (ties go shorter)


def test_progress_shows_nothing_due_instead_of_0_of_0_on_a_new_plan(tmp_path):
    c, _ = __import__("test_chat").web(tmp_path, __import__("test_chat").FakeChat())
    page = c.get("/progress").text
    assert "Nothing due yet" in page and "0<small> / 0" not in page


def test_changing_schedule_or_goal_replans_automatically_and_raises_the_ceiling(tmp_path):
    from test_chat import web
    c, db = web(tmp_path, FakeChat())
    udb = db.for_user(1)
    marathon = {**SETUP, "baseline": "25", "goal_preset": "Marathon", "race_date": str(TODAY + timedelta(weeks=24)),
                "av_mon": "on", "min_tue": "60", "min_thu": "60", "min_sat": "60", "min_sun": "60", "min_mon": "60"}
    c.post("/setup", data=marathon)
    c.post("/plan/generate"); wait_job(c)
    before = max(w["target_km"] for w in udb.get_weeks())
    r = c.post("/setup", data={**marathon, "min_sun": "150", "min_tue": "90", "min_thu": "90", "min_sat": "90", "min_mon": "90"}, follow_redirects=True)
    assert "Re-planning" in r.text or "Saved" in r.text
    job = wait_job(c)
    assert job["kind"] == "review" and job["status"] == "done" and "new goal and schedule" in job["message"]
    after = max(w["target_km"] for w in udb.get_weeks())
    assert after > before + 10 and max(w["long_run_km"] for w in udb.get_weeks()) >= 20
    assert udb.kv_get("volume_outlook")["peak_km"] == after
    # saving with no actual change doesn't spend a model call
    runs_before = len(udb.recent_log(50))
    c.post("/setup", data={**marathon, "min_sun": "150", "min_tue": "90", "min_thu": "90", "min_sat": "90", "min_mon": "90"})
    assert wait_job(c)["kind"] == "review" and len(udb.recent_log(50)) == runs_before
