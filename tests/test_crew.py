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
