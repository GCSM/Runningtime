"""Spotify: PKCE connect by pasting the redirect address, token handling, song matching, playlist creation, and the UI around it."""
import base64
import hashlib
import json
import re
import time
from datetime import timedelta
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from coach.config import Settings
from coach.db import DB
from coach.errors import UserError
from coach.music import PlaylistService
from coach.spotify import (MSG_FORBIDDEN, MSG_NETWORK, MSG_RATE, MSG_RECONNECT, SCOPES, SpotifyClient, SpotifyError, parse_pasted, pkce_pair)
from coach.web import create_app
from test_actions import Env, TODAY
from test_core import FakeStrava, signup, wait_job
from test_music import FakeDJ, FakeCatalogue, a_run
from test_scenarios import ReferenceCoach, SETUP

REDIRECT = "http://127.0.0.1:8888/callback"


# ------------------------------------------------------------------------------------------------ test double ---
class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    @property
    def content(self):
        return b"" if self._body is None else json.dumps(self._body).encode()

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSpotify:
    """A scripted Spotify: records every request, answers from a routing table of (method, path-prefix) -> Resp or list of Resp."""
    def __init__(self):
        self.calls, self.routes, self.token_calls = [], {}, []
        self.catalogue = {}                                   # (artist, title) -> {uri, ms}
        self.token = Resp(200, {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600})
        self.me = Resp(200, {"id": "u123", "display_name": "Ann Runner", "product": "premium"})

    def route(self, method, path, resp):
        self.routes[(method, path)] = resp

    def post(self, url, data=None, timeout=None):
        self.token_calls.append(dict(data))
        r = self.token
        return r.pop(0) if isinstance(r, list) else r

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        path = url.replace("https://api.spotify.com/v1", "")
        self.calls.append({"method": method, "path": path, "params": params, "json": json, "auth": (headers or {}).get("Authorization")})
        if self._net:
            raise httpx.ConnectError("down")
        if path == "/me":
            return self.me.pop(0) if isinstance(self.me, list) else self.me
        if path == "/search":
            q = params["q"]
            title, artist = re.search(r'track:"(.*?)" artist:"(.*?)"', q).groups()
            hit = self.catalogue.get((artist, title))
            items = [{"uri": hit["uri"], "name": title, "duration_ms": hit["ms"], "artists": [{"name": artist}]}] if hit else []
            return Resp(200, {"tracks": {"items": items}})
        r = self.routes.get((method, path))
        if r is None:
            if method == "POST" and path == "/me/playlists":
                return Resp(201, {"id": "PL1", "external_urls": {"spotify": "https://open.spotify.com/playlist/PL1"}})
            if method == "POST" and path.endswith("/items"):
                return Resp(201, {"snapshot_id": "x"})
            return Resp(404, {})
        return r.pop(0) if isinstance(r, list) else r

    _net = False


def client(tmp_path, fake=None, cid="c" * 32, uid_email="a@example.com"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    s = Settings(db_path=str(tmp_path / "sp.db"), spotify_client_id=cid, spotify_redirect_uri=REDIRECT)
    db = DB(s.db_path)
    uid = db.create_user(uid_email, "Ann Lee", "x")
    fake = fake or FakeSpotify()
    return SpotifyClient(s, db.for_user(uid), http=fake), fake, db


def connect(sp, fake):
    sp.begin()
    st = sp.db.kv_get("spotify_pkce")["state"]
    return sp.finish(f"{REDIRECT}?code={'x' * 40}&state={st}")


# ----------------------------------------------------------------------------------------------------- PKCE ------
def test_pkce_challenge_is_the_s256_of_the_verifier_per_rfc7636():
    for _ in range(20):
        v, c = pkce_pair()
        assert 43 <= len(v) <= 128 and re.fullmatch(r"[A-Za-z0-9_\-]+", v) and re.fullmatch(r"[A-Za-z0-9_\-]{43}", c)
        assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    assert len({pkce_pair()[0] for _ in range(50)}) == 50


def test_rfc7636_appendix_b_test_vector():
    import coach.spotify as S
    v = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert S._b64url(hashlib.sha256(v.encode()).digest()) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_begin_builds_the_authorize_url_and_remembers_the_secret_per_user(tmp_path):
    sp, fake, db = client(tmp_path)
    url = sp.begin()
    u, q = urlparse(url), parse_qs(urlparse(url).query)
    assert f"{u.scheme}://{u.netloc}{u.path}" == "https://accounts.spotify.com/authorize"
    assert q["client_id"] == ["c" * 32] and q["response_type"] == ["code"] and q["redirect_uri"] == [REDIRECT]
    assert q["scope"] == [SCOPES] == ["playlist-modify-private"] and q["code_challenge_method"] == ["S256"] and "client_secret" not in q
    pk = sp.db.kv_get("spotify_pkce")
    assert q["state"] == [pk["state"]] and q["code_challenge"] == [base64.urlsafe_b64encode(hashlib.sha256(pk["verifier"].encode()).digest()).decode().rstrip("=")]
    assert "verifier" not in url and pk["verifier"] not in url
    other = db.create_user("b@example.com", "B", "x")
    assert db.for_user(other).kv_get("spotify_pkce") is None
    unset = SpotifyClient(Settings(db_path=str(tmp_path / "x.db")), sp.db, http=fake)
    with pytest.raises(SpotifyError, match="isn't set up"):
        unset.begin()


# ------------------------------------------------------------------------------------------ pasted address ------
@pytest.mark.parametrize("pasted,expected", [
    (f"{REDIRECT}?code=AQD123abc_-XYZ&state=st1", ("AQD123abc_-XYZ", "st1")),
    (f"  {REDIRECT}?state=st1&code=AQD123abc_-XYZ \n", ("AQD123abc_-XYZ", "st1")),
    (f"<{REDIRECT}?code=AQD123abc_-XYZ&state=st1>", ("AQD123abc_-XYZ", "st1")),
    ("?code=AQD123abc_-XYZ&state=st1", ("AQD123abc_-XYZ", "st1")),
    ("code=AQD123abc_-XYZ&state=st1", ("AQD123abc_-XYZ", "st1")),
    (f"{REDIRECT}?code=AQD123abc_-XYZ", ("AQD123abc_-XYZ", "")),
    ("http://127.0.0.1/callback?code=AQD123abc_-XYZ&state=s#frag", ("AQD123abc_-XYZ", "s")),
    ("A" * 150, ("A" * 150, "")),                                                         # a bare code
])
def test_parse_pasted_accepts_the_shapes_people_actually_paste(pasted, expected):
    assert parse_pasted(pasted) == expected


@pytest.mark.parametrize("pasted,msg", [
    ("", "Paste the address"), ("   ", "Paste the address"),
    (f"{REDIRECT}?error=access_denied&state=s", "cancelled"), (f"{REDIRECT}?error=server_error", "server_error"),
    ("hello there", "doesn't look like"), ("https://example.com/", "doesn't look like"), ("short", "doesn't look like"),
])
def test_parse_pasted_explains_what_went_wrong(pasted, msg):
    with pytest.raises(SpotifyError, match=msg):
        parse_pasted(pasted)


# ------------------------------------------------------------------------------------------------ connecting ----
def test_finish_exchanges_the_code_with_the_verifier_and_no_secret_then_stores_tokens(tmp_path):
    sp, fake, db = client(tmp_path)
    sp.begin()
    pk = sp.db.kv_get("spotify_pkce")
    name = sp.finish(f"{REDIRECT}?code={'z' * 40}&state={pk['state']}")
    assert name == "Ann Runner" and sp.connected and sp.account_name() == "Ann Runner"
    body = fake.token_calls[0]
    assert body == {"client_id": "c" * 32, "grant_type": "authorization_code", "code": "z" * 40, "redirect_uri": REDIRECT, "code_verifier": pk["verifier"]}
    tok = sp.db.kv_get("spotify_token")
    assert tok["access_token"] == "AT1" and tok["refresh_token"] == "RT1" and tok["expires_at"] > time.time() + 3000 and tok["product"] == "premium"
    assert sp.db.kv_get("spotify_pkce") is None                                           # single use
    assert fake.calls[0]["auth"] == "Bearer AT1"
    with pytest.raises(SpotifyError, match="expired"):
        sp.finish(f"{REDIRECT}?code={'z' * 40}&state={pk['state']}")                     # replaying it fails


def test_finish_rejects_wrong_state_expired_attempts_and_never_calls_spotify_for_them(tmp_path):
    sp, fake, _ = client(tmp_path)
    with pytest.raises(SpotifyError, match="expired"):
        sp.finish(f"{REDIRECT}?code={'z' * 40}&state=abc")                               # nothing started
    sp.begin()
    with pytest.raises(SpotifyError, match="different connection attempt"):
        sp.finish(f"{REDIRECT}?code={'z' * 40}&state=somebody-elses")
    assert fake.token_calls == [] and not sp.connected
    sp.db.kv_set("spotify_pkce", {**sp.db.kv_get("spotify_pkce"), "at": time.time() - 3600})
    state = sp.db.kv_get("spotify_pkce")["state"]
    with pytest.raises(SpotifyError, match="expired"):
        sp.finish(f"{REDIRECT}?code={'z' * 40}&state={state}")
    assert fake.token_calls == [] and sp.db.kv_get("spotify_pkce") is None


def test_finish_failures_are_plain_and_leave_no_half_connection(tmp_path):
    sp, fake, _ = client(tmp_path)
    fake.token = Resp(400, {"error": "invalid_grant", "error_description": "Invalid authorization code"})
    sp.begin()
    with pytest.raises(SpotifyError, match="refused the login \\(400\\): Invalid authorization code"):
        sp.finish(f"{REDIRECT}?code={'z' * 40}&state=" + sp.db.kv_get("spotify_pkce")["state"])
    assert not sp.connected
    fake.token = Resp(429)
    sp.begin()
    with pytest.raises(SpotifyError, match="rate-limiting"):
        sp.finish("A" * 60)
    fake.token = Resp(200, {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600})
    fake.me = Resp(403, {})                                                               # the account isn't on the app's user list
    sp.begin()
    with pytest.raises(SpotifyError, match="User Management"):
        sp.finish("A" * 60)
    assert not sp.connected                                                              # a token we can't use isn't kept


def test_disconnect_forgets_tokens_and_pending_attempts(tmp_path):
    sp, fake, _ = client(tmp_path)
    connect(sp, fake)
    sp.begin()
    sp.disconnect()
    assert not sp.connected and sp.db.kv_get("spotify_pkce") is None


# ----------------------------------------------------------------------------------------------- token upkeep ---
def test_expired_access_tokens_refresh_and_keep_the_old_refresh_token_if_none_is_returned(tmp_path):
    sp, fake, _ = client(tmp_path)
    connect(sp, fake)
    sp.db.kv_set("spotify_token", {**sp.db.kv_get("spotify_token"), "expires_at": time.time() - 10})
    fake.token = Resp(200, {"access_token": "AT2", "expires_in": 3600})
    sp._api("GET", "/me")
    assert fake.token_calls[-1] == {"client_id": "c" * 32, "grant_type": "refresh_token", "refresh_token": "RT1"}
    tok = sp.db.kv_get("spotify_token")
    assert tok["access_token"] == "AT2" and tok["refresh_token"] == "RT1" and fake.calls[-1]["auth"] == "Bearer AT2"
    sp.db.kv_set("spotify_token", {**tok, "expires_at": time.time() - 10})
    fake.token = Resp(200, {"access_token": "AT3", "refresh_token": "RT2", "expires_in": 3600})
    sp._api("GET", "/me")
    assert sp.db.kv_get("spotify_token")["refresh_token"] == "RT2"


def test_a_revoked_refresh_token_means_reconnect_and_a_rejected_access_token_retries_once(tmp_path):
    sp, fake, _ = client(tmp_path)
    connect(sp, fake)
    sp.db.kv_set("spotify_token", {**sp.db.kv_get("spotify_token"), "expires_at": time.time() - 10})
    fake.token = Resp(400, {"error": "invalid_grant"})
    with pytest.raises(SpotifyError) as e:
        sp._api("GET", "/me")
    assert str(e.value) == MSG_RECONNECT and not sp.connected
    sp, fake, _ = client(tmp_path / "b")
    connect(sp, fake)
    fake.me = [Resp(401, {}), Resp(200, {"id": "u123"})]
    fake.token = Resp(200, {"access_token": "AT-new", "expires_in": 3600})
    assert sp._api("GET", "/me")["id"] == "u123" and sp.db.kv_get("spotify_token")["access_token"] == "AT-new"
    fake.me = Resp(401, {})
    with pytest.raises(SpotifyError) as e2:
        sp._api("GET", "/me")
    assert str(e2.value) == MSG_RECONNECT and not sp.connected                           # still 401 after a refresh: treat as revoked


def test_api_errors_become_actionable_messages(tmp_path):
    sp, fake, _ = client(tmp_path)
    connect(sp, fake)
    for status, expect in ((403, re.escape(MSG_FORBIDDEN)), (429, re.escape(MSG_RATE)), (500, r"request failed \(500\)")):
        fake.me = Resp(status, {})
        with pytest.raises(SpotifyError) as e:
            sp._api("GET", "/me")
        assert re.search(expect, str(e.value)), (status, str(e.value))
    fake._net = True
    with pytest.raises(SpotifyError) as e:
        sp._api("GET", "/me")
    assert str(e.value) == MSG_NETWORK
    fake._net = False
    sp2, fake2, _ = client(tmp_path / "n")
    with pytest.raises(SpotifyError, match="not connected"):
        sp2._api("GET", "/me")


# ------------------------------------------------------------------------------------------------- matching -----
def test_search_picks_the_matching_song_and_reports_its_real_length(tmp_path):
    sp, fake, _ = client(tmp_path)
    connect(sp, fake)
    fake.catalogue[("Daft Punk", "Stronger")] = {"uri": "spotify:track:abc", "ms": 223_500}
    m = sp.search_track("Daft Punk", "Stronger")
    assert m == {"uri": "spotify:track:abc", "seconds": 224, "artist": "Daft Punk", "title": "Stronger"}
    assert sp.search_track("Nobody", "Nothing") is None
    q = fake.calls[-1]["params"]
    assert q["type"] == "track" and q["limit"] <= 10 and 'track:"Nothing"' in q["q"]


def test_search_rejects_wrong_artist_wrong_title_and_non_track_uris(tmp_path):
    sp, fake, _ = client(tmp_path)
    connect(sp, fake)
    def with_items(items):
        fake.request_orig = fake.request
        sp._api = lambda *a, **k: {"tracks": {"items": items}}
    with_items([{"uri": "spotify:track:1", "name": "Stronger", "duration_ms": 200000, "artists": [{"name": "Some Cover Band"}]}])
    assert sp.search_track("Daft Punk", "Stronger") is None
    with_items([{"uri": "spotify:track:1", "name": "Harder Better", "duration_ms": 200000, "artists": [{"name": "Daft Punk"}]}])
    assert sp.search_track("Daft Punk", "Stronger") is None
    with_items([{"uri": "spotify:episode:1", "name": "Stronger", "duration_ms": 200000, "artists": [{"name": "Daft Punk"}]}])
    assert sp.search_track("Daft Punk", "Stronger") is None
    with_items([{"uri": "spotify:track:1", "name": "Stronger - Remastered 2011", "duration_ms": 200000, "artists": [{"name": "Daft Punk"}]},
                {"broken": True}])
    assert sp.search_track("Daft Punk", "Stronger")["seconds"] == 200


# ---------------------------------------------------------------------------------------------- playlists -------
def test_create_playlist_is_private_uses_the_current_endpoints_and_batches_at_100(tmp_path):
    sp, fake, _ = client(tmp_path)
    connect(sp, fake)
    uris = [f"spotify:track:{i}" for i in range(250)]
    out = sp.create_playlist("RunningTime · Tempo", "desc", uris)
    assert out == {"id": "PL1", "url": "https://open.spotify.com/playlist/PL1", "added": 250}
    posts = [c for c in fake.calls if c["method"] == "POST"]
    assert posts[0]["path"] == "/me/playlists" and posts[0]["json"]["public"] is False and posts[0]["json"]["name"] == "RunningTime · Tempo"
    adds = posts[1:]
    assert [c["path"] for c in adds] == ["/playlists/PL1/items"] * 3 and [len(c["json"]["uris"]) for c in adds] == [100, 100, 50]
    assert sum((c["json"]["uris"] for c in adds), []) == uris                              # order preserved
    with pytest.raises(SpotifyError, match="no songs"):
        sp.create_playlist("x", "y", [])


def test_create_playlist_falls_back_to_the_older_endpoints_when_the_new_ones_404(tmp_path):
    sp, fake, _ = client(tmp_path)
    connect(sp, fake)
    fake.route("POST", "/me/playlists", Resp(404, {}))
    fake.route("POST", "/users/u123/playlists", Resp(201, {"id": "OLD", "external_urls": {"spotify": "https://open.spotify.com/playlist/OLD"}}))
    fake.route("POST", "/playlists/OLD/items", Resp(404, {}))
    fake.route("POST", "/playlists/OLD/tracks", Resp(201, {}))
    out = sp.create_playlist("n", "d", ["spotify:track:1"])
    assert out["id"] == "OLD" and [c["path"] for c in fake.calls if c["method"] == "POST"] == ["/me/playlists", "/users/u123/playlists", "/playlists/OLD/items", "/playlists/OLD/tracks"]


def test_a_403_on_creation_explains_the_developer_mode_causes(tmp_path):
    sp, fake, _ = client(tmp_path)
    connect(sp, fake)
    fake.route("POST", "/me/playlists", Resp(403, {}))
    with pytest.raises(SpotifyError) as e:
        sp.create_playlist("n", "d", ["spotify:track:1"])
    assert "User Management" in str(e.value) and "Premium" in str(e.value)


# ----------------------------------------------------------------------------- playlist service integration -----
@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def connected_service(env, known=True):
    s = Settings(db_path=env.db.path, spotify_client_id="c" * 32, spotify_redirect_uri=REDIRECT)
    fake = FakeSpotify()
    sp = SpotifyClient(s, env.udb, http=fake)
    connect(sp, fake)
    dj = FakeDJ()
    if known:
        for i in range(60):
            for j in range(40):
                fake.catalogue[(f"Artist{i}", f"Song{i}-{j}")] = {"uri": f"spotify:track:{i}-{j}", "ms": (180 + (i * 7 + j * 13) % 100) * 1000}
    return PlaylistService(env.svc, dj, FakeCatalogue(), 10, sp), fake, sp


def test_when_connected_songs_are_matched_on_spotify_with_exact_lengths_and_uris(env):
    svc, fake, sp = connected_service(env)
    pl = svc.generate(a_run(env, "tempo")["id"])
    tracks = [t for s in pl["segments"] for t in s["tracks"]]
    assert pl["matched_on_spotify"] and all(t["uri"].startswith("spotify:track:") and t["verified"] for t in tracks) and pl["verified"] == pl["count"]
    assert abs(pl["error_s"]) <= 30 and pl["note"] == ""


def test_songs_spotify_cannot_find_are_dropped_not_added(env):
    svc, fake, sp = connected_service(env, known=False)
    for i in range(0, 60):
        for j in range(0, 40, 2):                                                          # only half the model's songs exist on Spotify
            fake.catalogue[(f"Artist{i}", f"Song{i}-{j}")] = {"uri": f"spotify:track:{i}-{j}", "ms": 200_000}
    pl = svc.generate(a_run(env, "easy")["id"])
    tracks = [t for s in pl["segments"] for t in s["tracks"]]
    assert tracks and all(int(t["title"].split("-")[1]) % 2 == 0 for t in tracks) and all(t["uri"] for t in tracks)
    fake.catalogue.clear()
    with pytest.raises(UserError, match="None of the suggested songs"):
        svc.generate(a_run(env, "easy")["id"])


def test_a_spotify_failure_while_building_falls_back_to_the_general_catalogue_and_says_so(env):
    svc, fake, sp = connected_service(env)
    fake._net = True
    pl = svc.generate(a_run(env, "easy")["id"])
    assert "Couldn't use Spotify this time" in pl["note"] and pl["matched_on_spotify"] is False and pl["count"] > 0


def test_save_to_spotify_creates_one_private_playlist_with_the_songs_in_order(env):
    svc, fake, sp = connected_service(env)
    w = a_run(env, "tempo")
    pl = svc.generate(w["id"])
    out = svc.save_to_spotify(w["id"])
    assert out["url"] == "https://open.spotify.com/playlist/PL1" and out["missing"] == [] and out["added"] == pl["count"]
    posts = [c for c in fake.calls if c["method"] == "POST"]
    assert posts[0]["json"]["public"] is False and w["title"] in posts[0]["json"]["name"] and "RunningTime" in posts[0]["json"]["name"]
    sent = sum((c["json"]["uris"] for c in posts[1:]), [])
    assert sent == [t["uri"] for s in pl["segments"] for t in s["tracks"]]
    n = len(fake.calls)
    assert svc.save_to_spotify(w["id"]) == out and len(fake.calls) == n                  # tapping again doesn't create a duplicate
    assert svc.get(w["id"])["spotify"]["id"] == "PL1"
    svc.generate(w["id"])
    assert "spotify" not in svc.get(w["id"])                                              # a rebuilt playlist is a new one


def test_save_matches_any_unmatched_songs_and_reports_the_ones_it_cannot_find(env):
    svc, fake, sp = connected_service(env)
    w = a_run(env, "easy")
    pl = svc.generate(w["id"])
    victim = pl["segments"][1]["tracks"][0]
    victim["uri"] = ""
    env.udb.save_playlist(w["id"], pl)
    fake.catalogue.pop((victim["artist"], victim["title"]))
    out = svc.save_to_spotify(w["id"])
    assert out["missing"] == [f"{victim['artist']} – {victim['title']}"] and out["added"] == pl["count"] - 1


def test_save_refuses_politely_when_it_cannot_work(env):
    svc, fake, sp = connected_service(env)
    w = a_run(env, "easy")
    with pytest.raises(UserError, match="Build the playlist first"):
        svc.save_to_spotify(w["id"])
    svc.generate(w["id"])
    env.udb.update_workout(w["id"], duration_min=w["duration_min"] + 15)
    with pytest.raises(UserError, match="changed since"):
        svc.save_to_spotify(w["id"])
    svc.generate(w["id"])
    fake.route("POST", "/me/playlists", Resp(403, {}))
    with pytest.raises(UserError, match="User Management"):
        svc.save_to_spotify(w["id"])
    assert "spotify" not in env.udb.get_playlist(w["id"])                                # nothing recorded for a failed save
    sp.disconnect()
    with pytest.raises(UserError, match="Connect Spotify first"):
        svc.save_to_spotify(w["id"])
    bare = PlaylistService(env.svc, FakeDJ(), FakeCatalogue(), 10, None)
    with pytest.raises(UserError, match="isn't set up"):
        bare.save_to_spotify(w["id"])


# ------------------------------------------------------------------------------------------------------ web -----
def web(tmp_path, cid="c" * 32, limit=5):
    fakes = {}
    s = Settings(db_path=str(tmp_path / "w.db"), sync_interval_min=0, spotify_client_id=cid, spotify_redirect_uri=REDIRECT, spotify_user_limit=limit)
    db = DB(s.db_path)

    def factory(udb):
        fake = fakes.setdefault(udb.uid, FakeSpotify())
        for i in range(60):
            for j in range(40):
                fake.catalogue[(f"Artist{i}", f"Song{i}-{j}")] = {"uri": f"spotify:track:{i}-{j}", "ms": (180 + (i * 7 + j * 13) % 100) * 1000}
        return SpotifyClient(s, udb, http=fake)

    app = create_app(s, db=db, coach=ReferenceCoach(), strava_factory=lambda u: FakeStrava(), clock=lambda: TODAY, playlist=FakeDJ(),
                     resolver=FakeCatalogue(), spotify_factory=factory)
    c = TestClient(app)
    signup(c)
    c.post("/setup", data={**SETUP, "baseline": "25"})
    c.post("/plan/generate")
    assert wait_job(c)["status"] == "done"
    return c, db, fakes


def pasted_for(db, uid=1, code="q" * 40):
    return f"{REDIRECT}?code={code}&state={db.for_user(uid).kv_get('spotify_pkce')['state']}"


def test_me_tab_shows_each_state(tmp_path):
    c, db, _ = web(tmp_path)
    assert "Connect Spotify" in c.get("/me").text and "0 of 5 accounts" in c.get("/me").text
    c2, _, _ = web(tmp_path / "x", cid="")
    assert "Spotify isn’t set up on this server" in c2.get("/me").text and "/spotify/connect" not in c2.get("/me").text
    page = c.get("/spotify/connect").text
    assert "https://accounts.spotify.com/authorize?" in page and "code_challenge=" in page and "Open Spotify" in page and 'name="pasted"' in page
    assert "code_verifier" not in page and db.for_user(1).kv_get("spotify_pkce")["verifier"] not in page
    r = c.post("/spotify/finish", data={"pasted": pasted_for(db)}, follow_redirects=True)
    assert "Spotify connected as Ann Runner" in r.text and "Disconnect Spotify" in r.text and "1 of 5" not in r.text
    assert "AT1" not in r.text and "RT1" not in r.text                                    # tokens never reach a page
    assert db.for_user(1).kv_get("spotify_token")["access_token"] == "AT1"
    d = c.post("/spotify/disconnect", follow_redirects=True)
    assert "Spotify disconnected" in d.text and db.for_user(1).kv_get("spotify_token") is None


def test_finish_errors_come_back_as_messages_and_unconfigured_servers_say_so(tmp_path):
    c, db, _ = web(tmp_path)
    c.get("/spotify/connect")
    assert "doesn&#39;t look like" in c.post("/spotify/finish", data={"pasted": "hello"}, follow_redirects=True).text
    assert "cancelled" in c.post("/spotify/finish", data={"pasted": f"{REDIRECT}?error=access_denied"}, follow_redirects=True).text
    assert "different connection attempt" in c.post("/spotify/finish", data={"pasted": f"{REDIRECT}?code={'q' * 40}&state=nope"}, follow_redirects=True).text
    c2, _, _ = web(tmp_path / "y", cid="")
    assert "isn&#39;t set up" in c2.get("/spotify/connect", follow_redirects=True).text


def test_the_server_wide_account_limit_is_enforced(tmp_path):
    c, db, fakes = web(tmp_path, limit=1)
    c.get("/spotify/connect")
    c.post("/spotify/finish", data={"pasted": pasted_for(db)})
    b = TestClient(c.app)
    signup(b, "b@example.com", "B")
    r = b.get("/spotify/connect", follow_redirects=True)
    assert "full (1 of 1" in r.text and db.for_user(2).kv_get("spotify_pkce") is None
    assert c.get("/spotify/connect").status_code == 200                                    # someone already connected can re-connect


def test_one_user_cannot_finish_with_anothers_attempt(tmp_path):
    c, db, _ = web(tmp_path)
    c.get("/spotify/connect")
    stolen = pasted_for(db)
    b = TestClient(c.app)
    signup(b, "b@example.com", "B")
    b.get("/spotify/connect")
    r = b.post("/spotify/finish", data={"pasted": stolen}, follow_redirects=True)
    assert "different connection attempt" in r.text and db.for_user(2).kv_get("spotify_token") is None and db.for_user(1).kv_get("spotify_token") is None


def test_playlist_page_walks_from_connect_to_open_in_spotify(tmp_path):
    c, db, fakes = web(tmp_path)
    w = next(x for x in db.for_user(1).get_workouts(TODAY + timedelta(days=1)) if x["type"] != "race")
    build = lambda: (c.post(f"/workout/{w['id']}/playlist", headers={"X-Requested-With": "fetch"}), wait_job(c))  # noqa: E731
    build()
    page = c.get(f"/workout/{w['id']}/playlist").text
    assert "Connect Spotify to save this" in page and f"/spotify/connect?next=/workout/{w['id']}/playlist" in page and "Copy song list" in page
    c.get(f"/spotify/connect?next=/workout/{w['id']}/playlist")
    r = c.post("/spotify/finish", data={"pasted": pasted_for(db)}, follow_redirects=True)
    assert "Spotify connected as" in r.text and r.url.path == f"/workout/{w['id']}/playlist"          # lands back where they started
    build()                                                                                        # rebuild now matches on Spotify
    page = c.get(f"/workout/{w['id']}/playlist").text
    assert "Save to Spotify" in page and "Connect Spotify to save" not in page
    c.post(f"/workout/{w['id']}/playlist/save", headers={"X-Requested-With": "fetch"})
    job = wait_job(c)
    assert job["kind"] == "spotify" and job["status"] == "done" and job["message"] == "Saved to your Spotify."
    page = c.get(f"/workout/{w['id']}/playlist").text
    assert "Open in Spotify" in page and "https://open.spotify.com/playlist/PL1" in page and "Save to Spotify" not in page
    created = [x for x in fakes[1].calls if x["method"] == "POST" and x["path"] == "/me/playlists"]
    assert len(created) == 1 and created[0]["json"]["public"] is False


def test_save_errors_surface_through_the_job_banner_and_next_links_are_safe(tmp_path):
    c, db, fakes = web(tmp_path)
    w = next(x for x in db.for_user(1).get_workouts(TODAY + timedelta(days=1)) if x["type"] != "race")
    c.get("/spotify/connect?next=//evil.example.com")
    r = c.post("/spotify/finish", data={"pasted": pasted_for(db)}, follow_redirects=False)
    assert r.headers["location"].startswith("/me")                                          # open redirects are refused
    c.post(f"/workout/{w['id']}/playlist", headers={"X-Requested-With": "fetch"}); wait_job(c)
    fakes[1].route("POST", "/me/playlists", Resp(403, {}))
    c.post(f"/workout/{w['id']}/playlist/save", headers={"X-Requested-With": "fetch"})
    job = wait_job(c)
    assert job["status"] == "error" and "User Management" in job["message"]


def test_spotify_data_is_deleted_with_the_account(tmp_path):
    c, db, _ = web(tmp_path)
    c.get("/spotify/connect")
    c.post("/spotify/finish", data={"pasted": pasted_for(db)})
    assert db.for_user(1).kv_get("spotify_token")
    c.post("/account/delete", data={"confirm": "a@example.com"})
    assert db.for_user(1).kv_get("spotify_token") is None and db.user_ids_with_kv_key("spotify_token") == []


def test_config_reads_spotify_settings_from_the_environment(monkeypatch):
    assert Settings().spotify_client_id == "" and Settings().spotify_redirect_uri == REDIRECT and Settings().spotify_user_limit == 5
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "  abc123  ")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "https://coach.example.com/cb")
    monkeypatch.setenv("SPOTIFY_USER_LIMIT", "3")
    s = Settings.from_env()
    assert (s.spotify_client_id, s.spotify_redirect_uri, s.spotify_user_limit) == ("abc123", "https://coach.example.com/cb", 3)
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "")
    assert Settings.from_env().spotify_redirect_uri == REDIRECT
