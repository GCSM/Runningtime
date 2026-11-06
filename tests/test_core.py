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
    db = DB(s.db_path)
    uid = db.create_user("a@example.com", "A", "x")
    clock = {"d": TODAY}
    service = CoachService(db.for_user(uid), s, FakeCoach(), FakeStrava(), clock=lambda: clock["d"])
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


FORM = {"goal_preset": "10K", "race_date": str(TODAY + timedelta(weeks=12)), "baseline": "15", "units": "km",
        "experience": "intermediate", "long_run_day": "sun", "av_tue": "on", "min_tue": "45", "av_thu": "on", "min_thu": "45",
        "av_sun": "on", "min_sun": "90", "goal_time": "50:00"}


def make_client(tmp_path, **kw):
    s = Settings(db_path=str(tmp_path / "w.db"), sync_interval_min=0, **kw)
    db = DB(s.db_path)
    app = create_app(s, db=db, coach=FakeCoach(), strava_factory=lambda udb: FakeStrava(), clock=lambda: TODAY)
    return TestClient(app), db


def signup(c, email="a@example.com", name="Ann", pw="correct-horse"):
    return c.post("/signup", data={"name": name, "email": email, "password": pw}, follow_redirects=False)


def test_auth_gate_and_signup(tmp_path):
    c, db = make_client(tmp_path)
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    assert c.get("/healthz").status_code == 200 and c.get("/login").status_code == 200
    assert c.get("/static/app.css").status_code == 200
    assert "at least 8" in c.post("/signup", data={"email": "a@b.co", "password": "short"}, follow_redirects=True).text
    r = signup(c)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert db.user_by_email("a@example.com")["pw_hash"].startswith("scrypt$")  # never stored in clear
    assert c.get("/setup").status_code == 200
    dup = signup(c, email="A@EXAMPLE.com", name="Dup")  # same email, different case
    assert dup.headers["location"].startswith("/signup?err=") and "already" in dup.headers["location"]
    c.post("/logout")
    assert c.get("/", follow_redirects=False).status_code == 303
    assert "Wrong email" in c.post("/login", data={"email": "a@example.com", "password": "nope-nope"}, follow_redirects=True).text
    assert c.post("/login", data={"email": "A@Example.com", "password": "correct-horse"}, follow_redirects=False).status_code == 303


def test_signup_controls(tmp_path):
    c, _ = make_client(tmp_path, allow_signups=False)
    assert "closed" in c.post("/signup", data={"email": "a@b.co", "password": "longenough"}, follow_redirects=True).text
    c, _ = make_client(tmp_path, invite_code="sesame")
    assert "invite code" in c.post("/signup", data={"email": "z@b.co", "password": "longenough", "invite": "nope"}, follow_redirects=True).text
    assert c.post("/signup", data={"email": "z@b.co", "password": "longenough", "invite": "sesame"}, follow_redirects=False).status_code == 303


def test_web_flow_and_user_isolation(tmp_path):
    a, db = make_client(tmp_path)
    signup(a)
    assert a.post("/setup", data=FORM, follow_redirects=False).status_code == 303
    r = a.post("/plan/generate", follow_redirects=True)
    assert "Your plan is ready" in r.text and "Today" in r.text
    for page in ("/plan", "/progress", "/me", "/"):
        assert a.get(page).status_code == 200
    assert "long-run day" in a.post("/setup", data={**FORM, "av_sun": ""}, follow_redirects=True).text
    assert a.post("/review", data={"note": "good"}, follow_redirects=True).status_code == 200
    assert a.post("/plan/generate", headers={"origin": "http://evil.example"}).status_code == 403

    # A second user in a second browser sees none of user A's data.
    b = TestClient(a.app)
    signup(b, "b@example.com", "Bob")
    assert b.get("/", follow_redirects=False).headers["location"] == "/setup"
    assert db.for_user(2).get_workouts() == [] and db.for_user(2).get_profile() is None
    assert len(db.for_user(1).get_workouts()) > 10
    r = b.post("/plan/generate", follow_redirects=True)
    assert str(r.url).endswith("/setup") and db.for_user(2).get_workouts() == []


def test_delete_account_removes_everything(tmp_path):
    c, db = make_client(tmp_path)
    signup(c)
    c.post("/setup", data=FORM)
    c.post("/plan/generate")
    assert "confirm" in c.post("/account/delete", data={"confirm": "wrong"}, follow_redirects=True).text.lower()
    assert c.post("/account/delete", data={"confirm": "a@example.com"}, follow_redirects=False).status_code == 303
    assert db.user_by_email("a@example.com") is None and db.for_user(1).get_workouts() == []
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"


def test_marathon_race_week_has_room_for_the_race():
    p = profile(goal_km=42.195, goal_label="Marathon", race_date=TODAY + timedelta(weeks=20))
    weeks = skel(p, baseline=40)
    assert weeks[-1].race_week and weeks[-1].target_km >= 42.195 + 6


# ---- the real Claude call (fake SDK client) --------------------------------------------
from types import SimpleNamespace as NS

from coach.llm import Coach


class FakeSDK:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = NS(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


def _reply(content, stop="tool_use"):
    return NS(stop_reason=stop, content=content)


PLAN = NS(type="tool_use", name="submit_plan", input={"summary": "hi", "workouts": [{"date": "2026-11-05"}]})


def test_claude_request_shape_is_valid_for_current_models():
    sdk = FakeSDK([_reply([PLAN])])
    out = Coach("claude-sonnet-5-5", client=sdk, effort="low").plan({"x": 1}, mode="generate")
    kw = sdk.calls[0]
    assert out == {"summary": "hi", "workouts": [{"date": "2026-11-05"}]}
    assert kw["tool_choice"] == {"type": "auto"}          # forced tool_choice is a 400 on current models
    assert kw["tools"][0]["strict"] is True and kw["tools"][0]["input_schema"]["additionalProperties"] is False
    assert kw["output_config"] == {"effort": "low"}
    assert "temperature" not in kw and "thinking" not in kw
    assert "submit_plan" in kw["messages"][0]["content"]


def test_claude_retries_once_without_tool_call_then_fails_clearly():
    sdk = FakeSDK([_reply([NS(type="text", text="sure!")], "end_turn"), _reply([PLAN])])
    assert Coach("m", client=sdk).plan({}, mode="adjust")["summary"] == "hi"
    assert len(sdk.calls) == 2
    sdk = FakeSDK([_reply([NS(type="text", text="no")], "end_turn")] * 2)
    with pytest.raises(RuntimeError, match="did not return a plan"):
        Coach("m", client=sdk).plan({}, mode="generate")


def test_claude_refusal_and_truncation_are_reported():
    with pytest.raises(RuntimeError, match="declined"):
        Coach("m", client=FakeSDK([_reply([], "refusal")])).plan({}, mode="generate")
    with pytest.raises(RuntimeError, match="cut off"):
        Coach("m", client=FakeSDK([_reply([], "max_tokens")])).plan({}, mode="generate")


# ---- friendly API-key / Anthropic error handling -------------------------------------------
import anthropic
import httpx2

from coach import check as check_mod
from coach.errors import CoachUnavailable, explain


def _api_error(cls, status, message="boom"):
    resp = httpx2.Response(status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    return cls(message, response=resp, body=None)


def test_explain_maps_known_failures_to_actionable_messages():
    assert "rejected the API key" in explain(_api_error(anthropic.AuthenticationError, 401))
    assert "out of credit" in explain(_api_error(anthropic.BadRequestError, 400, "Your credit balance is too low to access the API"))
    assert "COACH_MODEL" in explain(_api_error(anthropic.NotFoundError, 404))
    assert "rate limit" in explain(_api_error(anthropic.RateLimitError, 429)).lower()
    assert "internet" in explain(anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")))
    assert "No Anthropic API key" in explain(CoachUnavailable("No Anthropic API key is configured."))
    assert explain(ValueError("something else")) is None


def test_web_shows_friendly_message_for_rejected_key(tmp_path):
    class Rejecting(FakeCoach):
        def plan(self, ctx, *, mode, feedback=None):
            raise _api_error(anthropic.AuthenticationError, 401)

    s = Settings(db_path=str(tmp_path / "k.db"), sync_interval_min=0)
    db = DB(s.db_path)
    c = TestClient(create_app(s, db=db, coach=Rejecting(), strava_factory=lambda u: FakeStrava(), clock=lambda: TODAY))
    signup(c)
    c.post("/setup", data=FORM)
    r = c.post("/plan/generate", follow_redirects=True)
    assert "rejected the API key" in r.text and "Something went wrong" not in r.text


def test_missing_key_gives_clear_message_not_a_stack_trace(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = Settings(db_path=str(tmp_path / "m.db"), sync_interval_min=0)
    db = DB(s.db_path)
    c = TestClient(create_app(s, db=db, coach=Coach("claude-sonnet-5-5"), strava_factory=lambda u: FakeStrava(), clock=lambda: TODAY))
    signup(c)
    c.post("/setup", data=FORM)
    assert "No Anthropic API key is configured" in c.post("/plan/generate", follow_redirects=True).text


def test_check_tool_spots_common_paste_mistakes(tmp_path):
    assert check_mod.problems_with_key("sk-ant-api03-fine") == []
    assert any("quotes" in p for p in check_mod.problems_with_key('"sk-ant-abc"'))
    assert any("whitespace" in p for p in check_mod.problems_with_key(" sk-ant-abc"))
    assert any("middle" in p for p in check_mod.problems_with_key("sk-ant-ab cd"))
    prefix = check_mod.problems_with_key("sk-proj-abc")
    assert prefix and prefix[0].startswith("Note:")                      # unusual prefix is a note, not a failure
    assert check_mod.problems_with_key("") == ["ANTHROPIC_API_KEY is empty or missing."]
    f = tmp_path / "x.env"
    f.write_text("# c\nANTHROPIC_API_KEY=sk-ant-1\nCOACH_MODEL=m\n")
    assert check_mod.read_env_file(str(f)) == {"ANTHROPIC_API_KEY": "sk-ant-1", "COACH_MODEL": "m"}


def test_check_tool_reports_rejected_key_and_success(tmp_path, monkeypatch, capsys):
    f = tmp_path / "x.env"
    f.write_text("ANTHROPIC_API_KEY=sk-ant-badkey\nCOACH_MODEL=claude-sonnet-5-5\n")

    class Bad:
        def __init__(self, **kw): self.messages = NS(create=self._c)
        def _c(self, **kw): raise _api_error(anthropic.AuthenticationError, 401)

    monkeypatch.setattr(anthropic, "Anthropic", Bad)
    assert check_mod.main(["check", str(f)]) == 1
    out = capsys.readouterr().out
    assert "rejected the API key" in out and "sk-ant-" in out and "sk-ant-badkey" not in out   # key never printed in full

    class Good:
        def __init__(self, **kw): self.messages = NS(create=lambda **k: NS(stop_reason="end_turn"))

    monkeypatch.setattr(anthropic, "Anthropic", Good)
    assert check_mod.main(["check", str(f)]) == 0
    assert "accepted the key" in capsys.readouterr().out
    f.write_text("ANTHROPIC_API_KEY=odd-prefix-but-valid\n")          # API accepts it -> success despite the note
    assert check_mod.main(["check", str(f)]) == 0
    assert "Note:" in capsys.readouterr().out
    f.write_text("ANTHROPIC_API_KEY=\n")
    assert check_mod.main(["check", str(f)]) == 1


# ---- per-user Strava apps ---------------------------------------------------------------
from urllib.parse import parse_qs, urlparse

import coach.strava as strava_mod

SECRET_A = "a" * 40
SECRET_B = "b" * 40


def real_strava_client(tmp_path, **settings_kw):
    """App wired with the REAL StravaClient (no network is touched until a token exchange)."""
    s = Settings(db_path=str(tmp_path / "s.db"), sync_interval_min=0, public_url="http://coach.lan:8000", **settings_kw)
    db = DB(s.db_path)
    return TestClient(create_app(s, db=db, coach=FakeCoach(), clock=lambda: TODAY)), db


def authorize_params(c):
    r = c.get("/strava/connect", follow_redirects=False)
    assert r.status_code in (302, 307) and "strava.com/oauth/authorize" in r.headers["location"], r.headers.get("location")
    return {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}


def test_each_user_connects_through_their_own_strava_app(tmp_path):
    a, db = real_strava_client(tmp_path)                       # server has NO shared Strava app
    signup(a, "a@example.com", "A")
    assert "Client ID and Secret first" in a.get("/strava/connect", follow_redirects=True).text   # nothing configured yet
    assert "Save my Strava app" in a.get("/me").text

    assert "should be a number" in a.post("/strava/app", data={"client_id": "abc", "client_secret": SECRET_A}, follow_redirects=True).text
    assert "look right" in a.post("/strava/app", data={"client_id": "111", "client_secret": "short"}, follow_redirects=True).text
    assert a.post("/strava/app", data={"client_id": " 111 ", "client_secret": f" {SECRET_A} "}, follow_redirects=True).status_code == 200

    page = a.get("/me").text
    assert "111" in page and SECRET_A not in page              # the secret is never shown again
    assert "Connect with Strava" in page and "Change or remove my Strava app" in page
    assert "coach.lan" not in page or "Authorization Callback Domain" not in page      # setup steps are replaced once an app is saved
    params = authorize_params(a)
    assert params["client_id"] == "111" and params["redirect_uri"] == "http://coach.lan:8000/strava/callback"

    # A second user is unaffected, and can use a different app of their own.
    b = TestClient(a.app)
    signup(b, "b@example.com", "B")
    assert "Save my Strava app" in b.get("/me").text and "111" not in b.get("/me").text
    b.post("/strava/app", data={"client_id": "222", "client_secret": SECRET_B})
    assert authorize_params(b)["client_id"] == "222" and authorize_params(a)["client_id"] == "111"
    assert db.for_user(1).kv_get("strava_app")["client_secret"] == SECRET_A
    assert db.for_user(2).kv_get("strava_app")["client_secret"] == SECRET_B


def test_token_exchange_uses_the_users_own_credentials(tmp_path, monkeypatch):
    c, db = real_strava_client(tmp_path)
    signup(c)
    c.post("/strava/app", data={"client_id": "111", "client_secret": SECRET_A})
    state = authorize_params(c)["state"]
    posted = {}

    class Resp:
        def __init__(self, code, data): self.status_code, self._d, self.text = code, data, str(data)
        def json(self): return self._d

    def fake_post(url, data=None, timeout=None):
        posted.update(data)
        return Resp(200, {"access_token": "t", "refresh_token": "r", "expires_at": 9_999_999_999, "athlete": {"firstname": "Ann", "lastname": "R"}})

    monkeypatch.setattr(strava_mod.httpx, "post", fake_post)
    monkeypatch.setattr(strava_mod.httpx, "get", lambda *a, **k: Resp(200, []))
    r = c.get("/strava/callback", params={"code": "abc", "scope": "read,activity:read_all", "state": state}, follow_redirects=True)
    assert "Strava connected" in r.text, r.text[-500:]
    assert posted["client_id"] == "111" and posted["client_secret"] == SECRET_A and posted["code"] == "abc"
    assert db.for_user(1).kv_get("strava_token")["athlete_name"] == "Ann R"
    assert "Connected as Ann R" in c.get("/me").text


def test_changing_app_clears_old_token_but_resaving_same_app_keeps_it(tmp_path):
    c, db = real_strava_client(tmp_path)
    signup(c)
    c.post("/strava/app", data={"client_id": "111", "client_secret": SECRET_A})
    db.for_user(1).kv_set("strava_token", {"access_token": "t", "refresh_token": "r", "expires_at": 9e9, "athlete_name": "Ann"})
    c.post("/strava/app", data={"client_id": "111", "client_secret": SECRET_B})        # rotated secret, same app
    assert db.for_user(1).kv_get("strava_token") is not None
    c.post("/strava/app", data={"client_id": "999", "client_secret": SECRET_B})        # different app: old tokens are invalid
    assert db.for_user(1).kv_get("strava_token") is None
    db.for_user(1).kv_set("strava_token", {"access_token": "t", "refresh_token": "r", "expires_at": 9e9})
    c.post("/strava/app/remove")
    assert db.for_user(1).kv_get("strava_app") is None and db.for_user(1).kv_get("strava_token") is None


def test_shared_server_app_is_only_a_fallback(tmp_path):
    c, db = real_strava_client(tmp_path, strava_client_id="777", strava_client_secret="s" * 40)
    signup(c)
    assert "This server has a shared Strava app" in c.get("/me").text
    assert authorize_params(c)["client_id"] == "777"                                  # falls back to the server app
    c.post("/strava/app", data={"client_id": "111", "client_secret": SECRET_A})
    assert authorize_params(c)["client_id"] == "111"                                  # the user's own app wins


def test_deleting_account_removes_strava_credentials(tmp_path):
    c, db = real_strava_client(tmp_path)
    signup(c)
    c.post("/strava/app", data={"client_id": "111", "client_secret": SECRET_A})
    c.post("/account/delete", data={"confirm": "a@example.com"})
    assert db.for_user(1).kv_get("strava_app") is None
