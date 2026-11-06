"""Run playlists: shape follows the run, length is fitted by code, model output is validated, nothing leaks, limits hold."""
import random
from datetime import timedelta
from types import SimpleNamespace as NS

import httpx
import pytest
from fastapi.testclient import TestClient

from coach.config import Settings
from coach.db import DB
from coach.errors import CoachUnavailable, UserError
from coach.music import (DEFAULT_TRACK_S, MAX_TOKENS, SYSTEM, TOOL, DeezerResolver, PlaylistCoach, PlaylistService, Track, clean_candidates, fit,
                         signature, spotify_link)
from coach.runshape import ENERGY_BPM, Segment, segments_for, total_seconds
from coach.web import create_app
from test_actions import Env, TODAY
from test_core import FakeStrava, signup, wait_job
from test_scenarios import ReferenceCoach, SETUP


# ------------------------------------------------------------------------------------------------ run shape ------
@pytest.mark.parametrize("w", [dict(type=t, duration_min=m, title=ti) for t in ("easy", "long", "tempo", "intervals", "recovery", "race", "cross")
                               for m in (12, 25, 45, 75, 150, 240) for ti in ("", "8 × 400 m hard")])
def test_segments_always_add_up_to_exactly_the_run_and_are_sane(w):
    segs = segments_for(w)
    assert sum(s.seconds for s in segs) == w["duration_min"] * 60 == total_seconds(w)
    assert all(s.seconds > 0 and 1 <= s.energy <= 5 for s in segs)


def test_warmup_is_calmer_than_the_hard_part_and_cooldown_winds_down():
    for t in ("tempo", "intervals"):
        s = segments_for(dict(type=t, duration_min=60))
        assert s[0].name == "Warm-up" and s[-1].name == "Cool-down"
        assert s[0].energy < max(x.energy for x in s) and s[-1].energy < max(x.energy for x in s) and s[-1].energy <= s[0].energy
    assert max(x.energy for x in segments_for(dict(type="intervals", duration_min=60))) == 5
    long = segments_for(dict(type="long", duration_min=150))
    assert long[-1].energy == 1 and long[0].energy == 2 and max(s.energy for s in long) <= 4
    easy = segments_for(dict(type="easy", duration_min=45))
    assert max(s.energy for s in easy) <= 2
    assert segments_for(dict(type="recovery", duration_min=30))[0].energy == 1


def test_interval_reps_are_named_and_distance_only_runs_use_pace():
    s = segments_for(dict(type="intervals", duration_min=55, title="6 × 3 min hard"))
    assert any("6 reps" in x.name for x in s)
    assert total_seconds(dict(type="easy", distance_km=10, duration_min=0), 360) == 3600
    assert total_seconds(dict(type="easy", distance_km=0.5, duration_min=0)) == 600            # never below ten minutes
    assert all(ENERGY_BPM[a][0] < ENERGY_BPM[a + 1][0] for a in range(1, 5))                  # tempo targets rise with energy


# ---------------------------------------------------------------------------------------- the fitting maths -----
def pool(n, seed, sec=(150, 300)):
    r = random.Random(seed)
    return [Track(f"Artist{seed}-{i}", f"Song{seed}-{i}", r.randint(*sec), 150, True) for i in range(n)]


@pytest.mark.parametrize("seed", range(40))
def test_fit_lands_within_half_a_minute_of_the_run_across_many_shapes(seed):
    r = random.Random(seed)
    kind = r.choice(["easy", "tempo", "intervals", "long", "race"])
    mins = r.randint(20, 240)
    segs = segments_for(dict(type=kind, duration_min=mins))
    pools = [pool(max(3, int(s.seconds / 210 * 1.6) + 2), seed * 100 + i) for i, s in enumerate(segs)]
    chosen, err = fit(segs, pools)
    assert abs(err) <= 30, (kind, mins, err)
    assert err == sum(t.seconds for c in chosen for t in c) - mins * 60
    keys = [t.key for c in chosen for t in c]
    assert len(keys) == len(set(keys))                                                            # no repeats
    assert all(c for c, s in zip(chosen, segs) if s.seconds >= 90)                                # every real segment has music
    for c, p in zip(chosen, pools):
        assert all(t in p for t in c)                                                             # songs stay in the segment they were chosen for


def test_each_segment_gets_close_to_its_own_share_not_just_the_total():
    segs = [Segment("Warm-up", 600, 1), Segment("Hard", 1800, 5), Segment("Cool-down", 600, 1)]
    chosen, err = fit(segs, [pool(8, 1), pool(14, 2), pool(8, 3)])
    for s, c in zip(segs, chosen):
        assert abs(sum(t.seconds for t in c) - s.seconds) <= 150
    assert abs(err) <= 20


def test_fit_with_too_few_songs_is_honest_about_the_error_and_does_not_crash():
    segs = [Segment("Only", 3600, 3)]
    chosen, err = fit(segs, [[Track("A", "One", 200, 140), Track("B", "Two", 250, 140)]])
    assert len(chosen[0]) == 2 and err == 450 - 3600
    assert fit([Segment("Empty", 600, 2)], [[]]) == ([[]], -600)


def test_spotify_links_are_search_links_and_safely_encoded():
    link = spotify_link(Track("Guns N' Roses", "Sweet Child o' Mine / live?", 200, 120))
    assert link.startswith("https://open.spotify.com/search/") and " " not in link and "?" not in link.split("search/")[1] and "/live" not in link


# -------------------------------------------------------------------------------- validating the model's output -----
def test_candidates_are_validated_deduplicated_and_clamped():
    raw = {"segments": [
        {"index": 0, "tracks": [{"artist": "A", "title": "One", "seconds": 200, "bpm": 100},
                                {"artist": "A", "title": "One (Remastered)", "seconds": 200, "bpm": 100},       # same song
                                {"artist": "", "title": "No artist", "seconds": 200, "bpm": 100},
                                {"artist": "B", "title": "Silly length", "seconds": 99999, "bpm": 9999},
                                {"artist": "C", "title": "Text length", "seconds": "abc", "bpm": None}, "junk", None]},
        {"index": 1, "tracks": [{"artist": "A", "title": "One", "seconds": 200, "bpm": 100},                       # duplicate of segment 0
                                {"artist": "D", "title": "Fine", "seconds": 180, "bpm": 170}]},
        {"index": 9, "tracks": [{"artist": "E", "title": "Out of range", "seconds": 200, "bpm": 100}]},
        {"index": "x", "tracks": []}, "junk",
    ]}
    p = clean_candidates(raw, 2)
    assert [t.title for t in p[0]] == ["One", "Silly length", "Text length"] and [t.title for t in p[1]] == ["Fine"]
    assert p[0][1].seconds == DEFAULT_TRACK_S and p[0][1].bpm == 0 and p[0][2].seconds == DEFAULT_TRACK_S
    assert clean_candidates("nonsense", 3) == [[], [], []] and clean_candidates({"segments": "x"}, 1) == [[]]
    assert len(clean_candidates({"segments": [{"index": 0, "tracks": [{"artist": "A" * 500, "title": "T" * 500, "seconds": 200, "bpm": 100}]}]}, 1)[0][0].artist) == 80


# ---------------------------------------------------------------------------------------------- the model call ----
class FakeSDK:
    def __init__(self, content, stop="tool_use"):
        self.kwargs = None
        self.messages = NS(create=self._create)
        self._resp = NS(content=content, stop_reason=stop)

    def _create(self, **kw):
        self.kwargs = kw
        return self._resp


def tool_block(**inp):
    return NS(type="tool_use", name="build_playlist", input=inp)


def test_request_uses_the_cheap_model_low_effort_and_sends_only_run_shape_and_taste():
    sdk = FakeSDK([tool_block(segments=[{"index": 0, "tracks": [{"artist": "A", "title": "T", "seconds": 200, "bpm": 120}]}])])
    segs = [Segment("Warm-up", 300, 1), Segment("Hard", 900, 5)]
    out = PlaylistCoach("claude-haiku-5-5", "low", client=sdk).suggest(segs, {"likes": "90s hip hop", "favourites": ["Kanye West - Stronger"], "clean_only": True})
    kw = sdk.kwargs
    assert kw["model"] == "claude-haiku-5-5" and kw["output_config"] == {"effort": "low"} and kw["max_tokens"] == MAX_TOKENS
    assert kw["tool_choice"] == {"type": "auto"} and kw["tools"] == [TOOL] and kw["system"] == SYSTEM
    body = kw["messages"][0]["content"]
    assert "90s hip hop" in body and "Stronger" in body and '"clean_only":true' in body and '"energy":5' in body
    assert "@" not in body and "email" not in body.lower()                                  # nothing about the account or the athlete's plan
    assert out[0][0].title == "T" and out[1] == []


def test_model_failures_become_plain_messages(monkeypatch):
    with pytest.raises(UserError, match="didn't return a playlist"):
        PlaylistCoach("m", client=FakeSDK([NS(type="text", text="hi")], "end_turn")).suggest([Segment("x", 300, 1)], {})
    with pytest.raises(UserError, match="couldn't build"):
        PlaylistCoach("m", client=FakeSDK([], "refusal")).suggest([Segment("x", 300, 1)], {})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(CoachUnavailable, match="No Anthropic API key"):
        PlaylistCoach("m").suggest([Segment("x", 300, 1)], {})


def test_system_prompt_forbids_inventing_songs_and_treats_taste_as_data():
    s = SYSTEM.lower()
    assert "never invent" in s and "not instructions" in s and "real" in s and "clean_only" in s


# ------------------------------------------------------------------------------------------ real song lengths ----
class Http:
    def __init__(self, payload=None, status=200, raises=None):
        self.payload, self.status, self.raises, self.calls = payload, status, raises, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if self.raises:
            raise self.raises
        return NS(status_code=self.status, json=lambda: self.payload)


def hit(artist, title, dur):
    return {"title": title, "duration": dur, "artist": {"name": artist}}


def test_resolver_returns_the_catalogue_length_for_the_right_song():
    http = Http({"data": [hit("Other Band", "Stronger", 100), hit("Kanye West", "Stronger", 311)]})
    r = DeezerResolver(http)
    assert r.lookup("Kanye West", "Stronger") == 311
    assert "artist" in http.calls[0][1]["q"] and http.calls[0][0].startswith("https://api.deezer.com/")
    assert DeezerResolver(Http({"data": [hit("Daft Punk", "Stronger (Remastered 2009)", 223)]})).lookup("Daft Punk", "Stronger") == 223
    assert DeezerResolver(Http({"data": [hit("Someone Else", "Another Song", 200)]})).lookup("Kanye West", "Stronger") is None
    assert DeezerResolver(Http({"data": [hit("Kanye West", "Stronger", 5)]})).lookup("Kanye West", "Stronger") is None      # nonsense length
    assert DeezerResolver(Http({"data": []})).lookup("A", "B") is None


def test_resolver_degrades_quietly_and_stops_trying_when_offline():
    for bad in (Http(status=500), Http({"weird": 1}), Http({"data": [{"title": "x"}]}), Http(payload=None)):
        assert DeezerResolver(bad).lookup("A", "B") is None
    http = Http(raises=httpx.ConnectError("no route"))
    r = DeezerResolver(http)
    assert r.lookup("A", "B") is None and r.lookup("C", "D") is None and len(http.calls) == 1       # one failure, then no more waiting


# ----------------------------------------------------------------------------------------------- the service -----
class FakeDJ:
    """Stands in for the model: plenty of songs per segment with varied lengths."""
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def suggest(self, segments, prefs):
        self.calls.append((list(segments), prefs))
        if self.fail:
            raise self.fail
        return [[Track(f"Artist{i}", f"Song{i}-{j}", 170 + (j * 37 + i * 11) % 110, 120 + 10 * s.energy)
                 for j in range(int(s.seconds / 190 * 1.7) + 3)] for i, s in enumerate(segments)]


class FakeCatalogue:
    def __init__(self, known_every=2):
        self.n, self.known_every = 0, known_every

    def lookup(self, artist, title):
        self.n += 1
        return 200 + (self.n * 13) % 90 if self.n % self.known_every == 0 else None


def a_run(env, kind="tempo"):
    return next(w for w in env.udb.get_workouts(TODAY + timedelta(days=1)) if w["status"] == "planned" and w["type"] == kind)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def svc_for(env, dj=None, cat=None, limit=10):
    return PlaylistService(env.svc, dj or FakeDJ(), cat or FakeCatalogue(), limit)


def test_generate_builds_a_playlist_that_matches_the_run_and_its_shape(env):
    w = a_run(env, "tempo")
    pl = svc_for(env).generate(w["id"])
    assert abs(pl["error_s"]) <= 30 and pl["target_s"] == w["duration_min"] * 60 and pl["total_s"] == pl["target_s"] + pl["error_s"]
    names = [s["name"] for s in pl["segments"]]
    assert names[0] == "Warm-up" and names[-1] == "Cool-down"
    energies = [s["energy"] for s in pl["segments"]]
    assert energies[0] < max(energies) and energies[-1] < max(energies)
    flat = [t for s in pl["segments"] for t in s["tracks"]]
    assert pl["count"] == len(flat) and [t["start_s"] for t in flat] == sorted(t["start_s"] for t in flat) and flat[0]["start_s"] == 0
    assert all(b["start_s"] == a["start_s"] + a["seconds"] for a, b in zip(flat, flat[1:]))
    assert 0 < pl["verified"] < pl["count"] and pl["verified"] == sum(t["verified"] for t in flat)
    assert pl["text"].splitlines()[0] == f"{flat[0]['artist']} – {flat[0]['title']}" and all(t["link"].startswith("https://open.spotify.com/search/") for t in flat)
    assert env.udb.get_playlist(w["id"]) == pl and svc_for(env).get(w["id"])["stale"] is False


def test_real_catalogue_lengths_replace_the_models_estimates(env):
    w = a_run(env, "easy")
    pl = svc_for(env, cat=FakeCatalogue(known_every=1)).generate(w["id"])
    assert pl["verified"] == pl["count"] and abs(pl["error_s"]) <= 30
    pl2 = svc_for(env, cat=FakeCatalogue(known_every=10 ** 9)).generate(w["id"])           # offline: still works on estimates
    assert pl2["verified"] == 0 and pl2["count"] > 0 and abs(pl2["error_s"]) <= 30


def test_taste_is_saved_cleaned_and_passed_to_the_model(env):
    dj = FakeDJ()
    s = svc_for(env, dj)
    s.save_prefs("  " + "x" * 500, "- Kanye West - Stronger\n\n• The Killers - Mr. Brightside\nKanye West - Stronger\n" + "\n".join(f"A - {i}" for i in range(100)), True)
    p = s.prefs()
    assert len(p["likes"]) == 300 and p["clean_only"] is True and p["favourites"][:2] == ["Kanye West - Stronger", "The Killers - Mr. Brightside"]
    assert len(p["favourites"]) == 60 and len(set(p["favourites"])) == 60
    s.generate(a_run(env)["id"])
    assert dj.calls[0][1] == p
    assert svc_for(env).prefs() == p                                                       # persisted per user


def test_allowance_failures_and_bad_ids_cost_nothing_and_say_why(env):
    s = svc_for(env, limit=2)
    w = a_run(env)
    s.generate(w["id"]); s.generate(w["id"])
    with pytest.raises(UserError, match="2 playlists in the last 24 hours"):
        s.generate(w["id"])
    assert s.used_today() == 2 and len(s.llm.calls) == 2
    s3 = svc_for(env, FakeDJ(fail=RuntimeError("down")), limit=5)
    easy = a_run(env, "easy")["id"]
    with pytest.raises(RuntimeError):
        s3.generate(easy)
    assert s3.used_today() == 2 and env.udb.get_playlist(easy) is None                   # a failed build saves nothing and isn't counted
    with pytest.raises(UserError, match="wasn't found"):
        s3.generate(99999)
    env.svc.skip_workout(easy, "")
    with pytest.raises(UserError, match="skipped"):
        svc_for(env).generate(easy)


def test_an_empty_answer_from_the_model_is_an_error_not_an_empty_playlist(env):
    class Empty(FakeDJ):
        def suggest(self, segments, prefs):
            return [[] for _ in segments]
    with pytest.raises(UserError, match="any songs"):
        svc_for(env, Empty()).generate(a_run(env)["id"])


def test_changing_the_run_marks_the_playlist_stale(env):
    s = svc_for(env)
    w = a_run(env, "easy")
    s.generate(w["id"])
    assert s.get(w["id"])["stale"] is False
    env.udb.update_workout(w["id"], duration_min=w["duration_min"] + 20)
    assert s.get(w["id"])["stale"] is True and s.get(w["id"])["signature"] == signature(w)
    assert s.get(424242) is None


def test_playlists_of_deleted_workouts_are_pruned_and_users_are_isolated(env):
    s = svc_for(env)
    w1, w2 = a_run(env, "easy"), a_run(env, "tempo")
    s.generate(w1["id"]); s.generate(w2["id"])
    other = env.db.create_user("b@example.com", "B", "x")
    assert env.db.for_user(other).get_playlist(w1["id"]) is None
    with env.db.conn() as c:
        c.execute("delete from workouts where id=?", (w1["id"],))
    s.generate(w2["id"])
    assert env.udb.get_playlist(w1["id"]) is None and env.udb.get_playlist(w2["id"])


# ------------------------------------------------------------------------------------------------------ web -----
def web(tmp_path, dj=None, cat=None):
    s = Settings(db_path=str(tmp_path / "m.db"), sync_interval_min=0, playlist_daily_limit=3)
    db = DB(s.db_path)
    app = create_app(s, db=db, coach=ReferenceCoach(), strava_factory=lambda u: FakeStrava(), clock=lambda: TODAY, playlist=dj or FakeDJ(), resolver=cat or FakeCatalogue())
    c = TestClient(app)
    signup(c)
    c.post("/setup", data={**SETUP, "baseline": "25"})
    c.post("/plan/generate")
    assert wait_job(c)["status"] == "done"
    return c, db


def first_run(db, kind=None):
    return next(w for w in db.for_user(1).get_workouts(TODAY + timedelta(days=1)) if w["type"] != "race" and (kind is None or w["type"] == kind))


def test_page_offers_a_build_button_then_shows_the_fitted_playlist(tmp_path):
    c, db = web(tmp_path)
    w = first_run(db)
    page = c.get(f"/workout/{w['id']}/playlist").text
    assert "Build my playlist" in page and "3 playlists left today" in page and "Your music taste" in page
    r = c.post(f"/workout/{w['id']}/playlist", headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200
    job = wait_job(c)
    assert job["kind"] == "playlist" and job["status"] == "done" and job["message"] == "Your playlist is ready."
    page = c.get(f"/workout/{w['id']}/playlist").text
    assert "Copy song list" in page and "Rebuild" in page and "open.spotify.com/search/" in page and "2 playlists left today" in page
    assert "Warm-up" in page and 'class="plbar"' in page and "for a " in page


def test_prefs_form_saves_and_pages_link_to_the_playlist(tmp_path):
    c, db = web(tmp_path)
    w = first_run(db)
    r = c.post("/music/prefs", data={"likes": "synthwave", "favourites": "Kavinsky - Nightcall", "clean_only": "on", "next": f"/workout/{w['id']}/playlist"}, follow_redirects=True)
    assert "Music taste saved" in r.text and 'value="synthwave"' in r.text and "Kavinsky - Nightcall" in r.text and "checked" in r.text
    assert "//evil" not in c.post("/music/prefs", data={"likes": "x", "next": "//evil.com"}, follow_redirects=False).headers["location"]
    assert f"/workout/{w['id']}/playlist" in c.get("/plan").text
    today_w = [x for x in db.for_user(1).get_workouts(TODAY, TODAY) if x["type"] != "race"]
    if today_w and today_w[0]["status"] == "planned":
        assert f"/workout/{today_w[0]['id']}/playlist" in c.get("/").text


def test_limit_errors_and_unknown_workouts_are_reported(tmp_path):
    c, db = web(tmp_path)
    w = first_run(db)
    for _ in range(3):
        c.post(f"/workout/{w['id']}/playlist", headers={"X-Requested-With": "fetch"})
        wait_job(c)
    c.post(f"/workout/{w['id']}/playlist", headers={"X-Requested-With": "fetch"})
    job = wait_job(c)
    assert job["status"] == "error" and "3 playlists" in job["message"]
    assert "wasn&#39;t found" in c.get("/workout/99999/playlist", follow_redirects=True).text
    assert "wasn&#39;t found" in c.post("/workout/99999/playlist", follow_redirects=True).text


def test_one_user_cannot_see_or_build_for_anothers_workouts_and_data_goes_with_the_account(tmp_path):
    c, db = web(tmp_path)
    w = first_run(db)
    c.post(f"/workout/{w['id']}/playlist", headers={"X-Requested-With": "fetch"})
    wait_job(c)
    b = TestClient(c.app)
    signup(b, "b@example.com", "B")
    b.post("/setup", data={**SETUP, "baseline": "25"})
    assert "Copy song list" not in b.get(f"/workout/{w['id']}/playlist", follow_redirects=True).text
    assert "wasn&#39;t found" in b.post(f"/workout/{w['id']}/playlist", follow_redirects=True).text
    c.post("/account/delete", data={"confirm": "a@example.com"})
    assert db.for_user(1).get_playlist(w["id"]) is None


def test_config_and_deploy_knobs(tmp_path):
    import os
    assert Settings().playlist_daily_limit == 10
    old = os.environ.get("PLAYLISTS_PER_DAY")
    os.environ["PLAYLISTS_PER_DAY"] = "4"
    try:
        assert Settings.from_env().playlist_daily_limit == 4
    finally:
        os.environ.pop("PLAYLISTS_PER_DAY") if old is None else os.environ.__setitem__("PLAYLISTS_PER_DAY", old)
    assert "PLAYLISTS_PER_DAY" in open("deploy/reconfigure.sh").read() and "PLAYLISTS_PER_DAY=10" in open(".env.example").read()
