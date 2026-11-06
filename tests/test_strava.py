"""Strava: one server-wide app, one token set per user. No network: httpx is replaced with recorders."""
import time
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

import coach.strava as strava_mod
from coach.config import Settings
from coach.db import DB
from coach.strava import MSG_NETWORK, MSG_RATE, MSG_REVOKED, StravaClient, StravaError
from coach.web import create_app
from test_core import FORM, TODAY, FakeCoach, FakeStrava, signup, wait_job

GLOBAL_ID, GLOBAL_SECRET = "777", "s" * 40


def make(tmp_path, *, configured=True, limit=10, public_url="http://coach.lan"):
    kw = dict(strava_client_id=GLOBAL_ID, strava_client_secret=GLOBAL_SECRET) if configured else {}
    s = Settings(db_path=str(tmp_path / "s.db"), sync_interval_min=0, public_url=public_url, strava_athlete_limit=limit, **kw)
    db = DB(s.db_path)
    app = create_app(s, db=db, coach=FakeCoach(), clock=lambda: TODAY)       # the REAL StravaClient
    return TestClient(app), db, s


def new_user(c, email, name="U"):
    client = TestClient(c.app)
    signup(client, email, name)
    return client


def connect_params(c, host="coach.lan"):
    r = c.get("/strava/connect", headers={"host": host}, follow_redirects=False)
    assert r.status_code in (302, 303, 307) and "strava.com/oauth/authorize" in r.headers["location"], r.headers.get("location")
    return {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}


def give_token(db, uid, name="Ann", expires_at=None, refresh="r1"):
    db.for_user(uid).kv_set("strava_token", {"access_token": "t1", "refresh_token": refresh, "athlete_name": name,
                                             "expires_at": expires_at if expires_at is not None else time.time() + 3600})


class Resp:
    def __init__(self, code=200, data=None, text=""):
        self.status_code, self._d, self.text = code, data if data is not None else {}, text or str(data)

    def json(self):
        return self._d


class Recorder:
    """Stands in for httpx.post/get and records every call."""
    def __init__(self, post=None, get=None):
        self.posts, self.gets, self._post, self._get = [], [], post, get

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, dict(data or {}), dict(headers or {})))
        r = self._post(url, data) if callable(self._post) else self._post
        if isinstance(r, Exception):
            raise r
        return r if r is not None else Resp(200, {})

    def get(self, url, headers=None, timeout=None, params=None):
        self.gets.append((url, dict(headers or {}), dict(params or {})))
        r = self._get(params) if callable(self._get) else self._get
        if isinstance(r, Exception):
            raise r
        return r if r is not None else Resp(200, [])


def patch_http(monkeypatch, rec):
    monkeypatch.setattr(strava_mod.httpx, "post", rec.post)
    monkeypatch.setattr(strava_mod.httpx, "get", rec.get)
    return rec


TOKEN_OK = Resp(200, {"access_token": "new-a", "refresh_token": "new-r", "expires_at": time.time() + 21600,
                      "athlete": {"firstname": "Ann", "lastname": "Runner"}})


# ---------------------------------------------------------------- the global app drives everyone's connection ----
def test_every_user_connects_through_the_one_server_app(tmp_path):
    c, db, s = make(tmp_path)
    a = new_user(c, "a@example.com", "A")
    b = new_user(c, "b@example.com", "B")
    for u in (a, b):
        assert "Connect with Strava" in u.get("/me").text and "Client Secret" not in u.get("/me").text   # no credentials form
        p = connect_params(u)
        assert p["client_id"] == GLOBAL_ID and p["redirect_uri"] == "http://coach.lan/strava/callback"
        assert p["scope"] == "read,activity:read_all" and p["state"]
    assert connect_params(a)["state"] != connect_params(b)["state"]            # states are per user/attempt


def test_removed_per_user_credential_routes_are_gone(tmp_path):
    c, _, _ = make(tmp_path)
    u = new_user(c, "a@example.com")
    assert u.post("/strava/app", data={"client_id": "1", "client_secret": "x" * 20}).status_code in (404, 405)
    assert u.post("/strava/app/remove").status_code in (404, 405)


def test_when_the_server_has_no_strava_app_users_are_told_and_the_operator_gets_instructions(tmp_path):
    c, _, _ = make(tmp_path, configured=False)
    u = new_user(c, "a@example.com")
    page = u.get("/me", headers={"host": "coach.lan"}).text
    assert "Strava isn’t set up on this server yet" in page and "For the person running the server" in page
    assert "<code>coach.lan</code>" in page and "STRAVA_ATHLETE_LIMIT" in page
    assert "Connect with Strava" not in page.replace("connect_with", "")       # no dead button
    r = u.get("/strava/connect", follow_redirects=True)
    assert "isn&#39;t set up on this server" in r.text or "isn't set up on this server" in r.text
    u.post("/setup", data=FORM)                                                 # Today only renders once there is a profile
    assert "Today" in u.get("/").text and "Link your account" not in u.get("/").text   # no nagging about something unavailable
    c2, _, _ = make(tmp_path / "ready")
    v = new_user(c2, "b@example.com")
    v.post("/setup", data=FORM)
    assert "Link your account so your plan adapts" in v.get("/").text           # …but it does prompt when Strava is available


def test_operator_hint_warns_about_ip_addresses(tmp_path):
    c, _, _ = make(tmp_path, configured=False)
    u = new_user(c, "a@example.com")
    assert "redirect_uri invalid" in u.get("/me", headers={"host": "192.168.1.50"}).text
    assert "redirect_uri invalid" not in u.get("/me", headers={"host": "coach.lan"}).text


def test_callback_follows_the_address_in_use_not_a_stale_public_url(tmp_path):
    c, _, _ = make(tmp_path, public_url="http://old.example")
    u = new_user(c, "a@example.com")
    assert connect_params(u, host="coach.new.lan")["redirect_uri"] == "http://coach.new.lan/strava/callback"


# --------------------------------------------------------------------------------------- OAuth callback -------
def test_callback_exchanges_the_code_with_the_global_credentials_and_stores_tokens_per_user(tmp_path, monkeypatch):
    c, db, _ = make(tmp_path)
    u = new_user(c, "a@example.com")
    state = connect_params(u)["state"]
    rec = patch_http(monkeypatch, Recorder(post=TOKEN_OK, get=Resp(200, [])))
    r = u.get("/strava/callback", params={"code": "abc", "scope": "read,activity:read_all", "state": state}, follow_redirects=True)
    assert "Strava connected" in r.text
    url, data, _ = rec.posts[0]
    assert url.endswith("/oauth/token") and data["client_id"] == GLOBAL_ID and data["client_secret"] == GLOBAL_SECRET
    assert data["code"] == "abc" and data["grant_type"] == "authorization_code"
    assert db.for_user(1).kv_get("strava_token")["athlete_name"] == "Ann Runner"
    assert "Connected as Ann Runner" in u.get("/me").text
    assert db.for_user(2).kv_get("strava_token") is None                       # tokens never cross accounts


def test_callback_rejects_a_wrong_state_and_a_missing_activity_scope(tmp_path, monkeypatch):
    c, db, _ = make(tmp_path)
    u = new_user(c, "a@example.com")
    rec = patch_http(monkeypatch, Recorder(post=TOKEN_OK))
    connect_params(u)
    assert "expired" in u.get("/strava/callback", params={"code": "x", "scope": "read,activity:read_all", "state": "forged"}, follow_redirects=True).text
    state = connect_params(u)["state"]
    assert "allow access to your activities" in u.get("/strava/callback", params={"code": "x", "scope": "read", "state": state}, follow_redirects=True).text
    assert not rec.posts and db.for_user(1).kv_get("strava_token") is None


def test_a_stale_per_user_app_from_the_old_design_is_ignored_and_cleaned_up(tmp_path, monkeypatch):
    c, db, _ = make(tmp_path)
    u = new_user(c, "a@example.com")
    db.for_user(1).kv_set("strava_app", {"client_id": "111", "client_secret": "z" * 40})
    assert connect_params(u)["client_id"] == GLOBAL_ID                         # global app wins
    state = connect_params(u)["state"]
    patch_http(monkeypatch, Recorder(post=TOKEN_OK, get=Resp(200, [])))
    u.get("/strava/callback", params={"code": "abc", "scope": "read,activity:read_all", "state": state})
    assert db.for_user(1).kv_get("strava_app") is None


# ------------------------------------------------------------------------------------------ the athlete limit ---
def test_connecting_is_refused_politely_when_the_apps_athlete_slots_are_full(tmp_path, monkeypatch):
    c, db, _ = make(tmp_path, limit=2)
    users = [new_user(c, f"u{i}@example.com") for i in range(3)]
    give_token(db, 1)
    give_token(db, 2)
    r = users[2].get("/strava/connect", follow_redirects=False)
    assert r.status_code == 303 and "strava.com" not in r.headers["location"]       # never sent to Strava
    page = users[2].get("/strava/connect", follow_redirects=True).text
    assert "full (2 of 2 athletes connected)" in page
    me = users[2].get("/me").text
    assert "This server’s Strava app is full" in me and "Connect with Strava" not in me
    assert "Connected as" in users[0].get("/me").text                              # people already connected are unaffected
    assert users[0].get("/strava/connect", follow_redirects=False).status_code in (302, 303, 307)   # …and may reconnect
    # A disconnect frees the slot (deauthorize is best effort and is recorded here).
    rec = patch_http(monkeypatch, Recorder(post=Resp(200, {})))
    users[1].post("/strava/disconnect")
    assert users[2].get("/strava/connect", follow_redirects=False).headers["location"].startswith("https://www.strava.com/oauth/authorize")
    assert any(u.endswith("/oauth/deauthorize") for u, _, _ in rec.posts)


def test_limit_defaults_to_ten_and_is_configurable():
    assert Settings().strava_athlete_limit == 10
    import os
    old = os.environ.get("STRAVA_ATHLETE_LIMIT")
    os.environ["STRAVA_ATHLETE_LIMIT"] = "3"
    try:
        assert Settings.from_env().strava_athlete_limit == 3
    finally:
        os.environ.pop("STRAVA_ATHLETE_LIMIT") if old is None else os.environ.__setitem__("STRAVA_ATHLETE_LIMIT", old)


# ------------------------------------------------------------------------ disconnecting and account deletion ---
def test_disconnect_revokes_on_strava_then_forgets_tokens(tmp_path, monkeypatch):
    c, db, _ = make(tmp_path)
    u = new_user(c, "a@example.com")
    give_token(db, 1)
    rec = patch_http(monkeypatch, Recorder(post=Resp(200, {})))
    u.post("/strava/disconnect")
    url, data, headers = rec.posts[0]
    assert url == "https://www.strava.com/oauth/deauthorize" and data["access_token"] == "t1" and headers["Authorization"] == "Bearer t1"
    assert db.for_user(1).kv_get("strava_token") is None
    assert "Connect with Strava" in u.get("/me").text


def test_disconnect_still_works_when_strava_is_unreachable(tmp_path, monkeypatch):
    c, db, _ = make(tmp_path)
    u = new_user(c, "a@example.com")
    give_token(db, 1)
    patch_http(monkeypatch, Recorder(post=httpx.ConnectError("no network")))
    assert u.post("/strava/disconnect", follow_redirects=True).status_code == 200
    assert db.for_user(1).kv_get("strava_token") is None


def test_deleting_an_account_revokes_strava_access_and_removes_everything(tmp_path, monkeypatch):
    c, db, _ = make(tmp_path)
    u = new_user(c, "a@example.com")
    give_token(db, 1)
    rec = patch_http(monkeypatch, Recorder(post=Resp(200, {})))
    assert u.post("/account/delete", data={"confirm": "a@example.com"}, follow_redirects=False).status_code == 303
    assert any(url.endswith("/oauth/deauthorize") for url, _, _ in rec.posts)
    assert db.user_by_email("a@example.com") is None and db.for_user(1).kv_get("strava_token") is None
    assert db.strava_user_ids() == []                                            # the slot is free again


# ------------------------------------------------------------------------- failures become clear messages ------
def client_for(tmp_path, token_expires=None, refresh="r1"):
    s = Settings(db_path=str(tmp_path / "k.db"), strava_client_id=GLOBAL_ID, strava_client_secret=GLOBAL_SECRET)
    db = DB(s.db_path)
    uid = db.create_user("a@example.com", "A", "x")
    give_token(db, uid, expires_at=token_expires, refresh=refresh)
    return StravaClient(s, db.for_user(uid)), db.for_user(uid)


def test_expired_token_is_refreshed_with_the_global_credentials(tmp_path, monkeypatch):
    sc, udb = client_for(tmp_path, token_expires=time.time() - 10)
    rec = patch_http(monkeypatch, Recorder(post=TOKEN_OK, get=Resp(200, [])))
    sc.fetch_runs(0)
    _, data, _ = rec.posts[0]
    assert data["grant_type"] == "refresh_token" and data["refresh_token"] == "r1" and data["client_id"] == GLOBAL_ID
    assert udb.kv_get("strava_token")["access_token"] == "new-a" and rec.gets[0][1]["Authorization"] == "Bearer new-a"


def test_access_revoked_on_strava_clears_the_connection_and_says_to_reconnect(tmp_path, monkeypatch):
    sc, udb = client_for(tmp_path)
    rec = patch_http(monkeypatch, Recorder(get=Resp(401, text="Authorization Error"), post=Resp(400, text="bad refresh token")))
    with pytest.raises(StravaError) as e:
        sc.fetch_runs(0)
    assert str(e.value) == MSG_REVOKED
    assert udb.kv_get("strava_token") is None and not sc.connected
    assert len(rec.gets) == 1 and len(rec.posts) == 1                              # tried one refresh, then gave up


def test_a_401_that_survives_a_successful_refresh_also_clears_the_connection(tmp_path, monkeypatch):
    sc, udb = client_for(tmp_path)
    rec = patch_http(monkeypatch, Recorder(get=Resp(401), post=TOKEN_OK))           # refresh works, but Strava still says no
    with pytest.raises(StravaError) as e:
        sc.fetch_runs(0)
    assert str(e.value) == MSG_REVOKED and udb.kv_get("strava_token") is None
    assert len(rec.gets) == 2 and len(rec.posts) == 1                                # exactly one retry, no loop


def test_a_401_that_a_refresh_fixes_is_retried_once(tmp_path, monkeypatch):
    sc, udb = client_for(tmp_path)
    calls = iter([Resp(401), Resp(200, [{"id": 1, "type": "Run", "distance": 5000, "moving_time": 1500, "start_date_local": "2026-11-03T07:00:00Z"}])])
    patch_http(monkeypatch, Recorder(get=lambda params: next(calls), post=TOKEN_OK))
    runs = sc.fetch_runs(0)
    assert len(runs) == 1 and runs[0]["date"] == "2026-11-03" and sc.connected


def test_rate_limit_is_reported_and_the_connection_is_kept(tmp_path, monkeypatch):
    sc, udb = client_for(tmp_path)
    patch_http(monkeypatch, Recorder(get=Resp(429, text="Rate Limit Exceeded")))
    with pytest.raises(StravaError) as e:
        sc.fetch_runs(0)
    assert str(e.value) == MSG_RATE and sc.connected


def test_network_failures_are_reported_clearly(tmp_path, monkeypatch):
    sc, _ = client_for(tmp_path)
    patch_http(monkeypatch, Recorder(get=httpx.ConnectError("down")))
    with pytest.raises(StravaError) as e:
        sc.fetch_runs(0)
    assert str(e.value) == MSG_NETWORK and sc.connected


def test_pagination_collects_every_page_and_only_runs(tmp_path, monkeypatch):
    sc, _ = client_for(tmp_path)
    page1 = [{"id": i, "type": "Run", "distance": 5000, "moving_time": 1500, "start_date_local": "2026-11-01T07:00:00Z"} for i in range(100)]
    page2 = [{"id": 1000, "type": "Ride", "distance": 20000, "moving_time": 3000, "start_date_local": "2026-11-02T07:00:00Z"},
             {"id": 1001, "sport_type": "TrailRun", "type": "Run", "distance": 9000, "moving_time": 3000, "start_date_local": "2026-11-02T07:00:00Z"}]
    pages = iter([Resp(200, page1), Resp(200, page2)])
    rec = patch_http(monkeypatch, Recorder(get=lambda params: next(pages)))
    runs = sc.fetch_runs(0)
    assert len(runs) == 101 and {r["id"] for r in runs} >= {0, 99, 1001} and 1000 not in {r["id"] for r in runs}
    assert [g[2]["page"] for g in rec.gets] == [1, 2]


def test_a_refused_token_exchange_shows_strava_s_own_message(tmp_path, monkeypatch):
    c, db, _ = make(tmp_path)
    u = new_user(c, "a@example.com")
    state = connect_params(u)["state"]
    patch_http(monkeypatch, Recorder(post=Resp(403, text='{"message":"Limit of connected athletes exceeded"}')))
    page = u.get("/strava/callback", params={"code": "x", "scope": "read,activity:read_all", "state": state}, follow_redirects=True).text
    assert "Strava refused the request (403)" in page and "connected athletes" in page
    assert db.for_user(1).kv_get("strava_token") is None


def test_background_sync_of_a_revoked_user_is_a_warning_not_a_crash(tmp_path, monkeypatch, caplog):
    c, db, s = make(tmp_path)
    new_user(c, "a@example.com")
    give_token(db, 1)
    patch_http(monkeypatch, Recorder(get=Resp(401), post=Resp(400)))
    from coach.service import CoachService
    svc = CoachService(db.for_user(1), s, FakeCoach(), StravaClient(s, db.for_user(1)), clock=lambda: TODAY)
    with pytest.raises(StravaError):
        svc.background_tick()
    assert db.for_user(1).kv_get("strava_token") is None                         # next tick won't retry a dead connection
    svc.background_tick()                                                         # not connected any more: silently skipped
