"""Run detail page: route drawing, splits, planned-vs-actual, Strava detail fetch + caching, privacy."""
import math
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

import coach.strava as strava_mod
from coach import polyline as P
from coach.config import Settings
from coach.db import DB
from coach.runview import build_run_view
from coach.strava import MSG_NETWORK, MSG_RATE, MSG_REVOKED, StravaClient, StravaError, trim_activity_detail
from coach.web import create_app
from test_actions import Env, TODAY
from test_core import FORM, FakeStrava, signup, wait_job
from test_scenarios import ReferenceCoach, SETUP, run_activity
from test_strava import GLOBAL_ID, GLOBAL_SECRET, Recorder, Resp, give_token, patch_http

GOOGLE_EXAMPLE = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"


# ------------------------------------------------------------------------------------------------ polyline ---
def test_decoder_matches_googles_documented_example():
    assert P.decode(GOOGLE_EXAMPLE) == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
    assert P.decode("") == []


def test_decoder_survives_truncated_input():
    full = P.decode(GOOGLE_EXAMPLE)
    for n in range(len(GOOGLE_EXAMPLE) + 1):                                   # cut the string at EVERY length: never an exception
        got = P.decode(GOOGLE_EXAMPLE[:n])
        assert got == full[:len(got)], n                                       # …and what decodes is always a clean prefix of the route
    assert P.decode(GOOGLE_EXAMPLE[:12]) == [(38.5, -120.2)]                   # a dangling half-pair is dropped


def encode(points):
    """Test-only encoder (inverse of decode) so we can build routes of any shape."""
    out, plat, plon = [], 0, 0
    for lat, lon in points:
        for v in (round(lat * 1e5) - plat, round(lon * 1e5) - plon):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = round(lat * 1e5), round(lon * 1e5)
    return "".join(out)


def test_encode_decode_roundtrip_for_the_test_helper():
    pts = [(51.5007, -0.1246), (51.5014, -0.1419), (51.5033, -0.1195)]
    got = P.decode(encode(pts))
    assert len(got) == 3 and all(abs(a - c) < 1e-5 and abs(b - d) < 1e-5 for (a, b), (c, d) in zip(got, pts))


def coords(d):
    return [tuple(float(n) for n in seg.strip("ML ").split()) for seg in d.replace("L", " L").split(" L")]


def test_svg_fits_the_box_keeps_aspect_and_puts_north_up():
    wide = [(51.5, -0.30 + i * 0.01) for i in range(20)]                      # runs east: wide and flat
    svg = P.to_svg(wide, 320, 200, pad=16)
    xs = [x for x, _ in [tuple(map(float, c.replace("M", "").split())) for c in svg["d"].split(" L")]]
    ys = [y for _, y in [tuple(map(float, c.replace("M", "").split())) for c in svg["d"].split(" L")]]
    assert min(xs) >= 16 - 0.2 and max(xs) <= 304 + 0.2 and min(ys) >= 16 - 0.2 and max(ys) <= 184 + 0.2
    assert (max(xs) - min(xs)) > 10 * (max(ys) - min(ys) + 0.1)               # stays wide, not stretched to fill the height
    north = [(51.50 + i * 0.01, -0.1) for i in range(10)]
    s2 = P.to_svg(north)
    assert s2["start"][1] > s2["end"][1]                                       # the start (south) is lower on screen than the end (north)


def test_longitude_is_shrunk_by_latitude():
    # 0.1° east at 60°N is half as far as 0.1° north, so a square of degrees must come out taller than wide.
    box = [(60.0, 10.0), (60.1, 10.0), (60.1, 10.1), (60.0, 10.1), (60.0, 10.0)]
    svg = P.to_svg(box, 400, 400, pad=0)
    pts = [tuple(map(float, c.replace("M", "").split())) for c in svg["d"].split(" L")]
    w = max(p[0] for p in pts) - min(p[0] for p in pts)
    h = max(p[1] for p in pts) - min(p[1] for p in pts)
    assert w / h == pytest.approx(math.cos(math.radians(60.05)), abs=0.02)


def test_svg_refuses_what_cant_be_drawn_and_thins_dense_routes():
    assert P.to_svg([]) is None and P.to_svg([(1, 1)]) is None and P.to_svg([(1, 1), (1, 1)]) is None
    dense = [(51.5 + i * 1e-5, -0.1 + (i % 7) * 1e-5) for i in range(5000)]
    svg = P.to_svg(dense)
    assert svg["d"].count("L") <= 401


# ----------------------------------------------------------------------------------------- trimming detail ---
def raw_detail(**kw):
    base = {"id": 5, "name": "Morning Run", "description": "felt fine", "distance": 8100.0, "moving_time": 2900, "elapsed_time": 3000,
            "total_elevation_gain": 42.0, "average_heartrate": 151.2, "max_heartrate": 172.0, "average_speed": 2.79,
            "start_date_local": "2026-11-03T07:00:00Z", "sport_type": "Run", "type": "Run", "device_name": "Garmin",
            "map": {"polyline": GOOGLE_EXAMPLE, "summary_polyline": "xxxx"},
            "splits_metric": [{"split": 1, "distance": 1000.0, "moving_time": 360, "elapsed_time": 362, "average_heartrate": 140.0, "elevation_difference": 3.0, "average_speed": 2.7},
                              {"split": 2, "distance": 1000.0, "moving_time": 350, "elapsed_time": 350, "elevation_difference": -1.0}],
            "photos": {"count": 3, "primary": {"urls": {"600": "https://private.example/p.jpg"}}}, "segment_efforts": [{"id": 1}],
            "athlete": {"id": 99}, "private_note": "secret"}
    base.update(kw)
    return base


def test_trimming_keeps_only_what_the_page_shows():
    t = trim_activity_detail(raw_detail())
    assert t["polyline"] == GOOGLE_EXAMPLE and t["name"] == "Morning Run" and t["max_heartrate"] == 172.0
    assert [s["split"] for s in t["splits"]] == [1, 2] and set(t["splits"][0]) == {"split", "distance", "moving_time", "elapsed_time", "average_heartrate", "elevation_difference"}
    for dropped in ("photos", "segment_efforts", "athlete", "private_note", "id", "map"):
        assert dropped not in t
    assert trim_activity_detail(raw_detail(map={"summary_polyline": "abc"}))["polyline"] == "abc"      # falls back to the summary line
    assert trim_activity_detail({"name": "x"}) == {"name": "x", "polyline": "", "splits": []}


# ------------------------------------------------------------------------------- Strava client: one activity ---
def client_for(tmp_path, expires=None):
    s = Settings(db_path=str(tmp_path / "k.db"), strava_client_id=GLOBAL_ID, strava_client_secret=GLOBAL_SECRET)
    db = DB(s.db_path)
    uid = db.create_user("a@example.com", "A", "x")
    give_token(db, uid, expires_at=expires)
    return StravaClient(s, db.for_user(uid)), db.for_user(uid)


def test_fetch_activity_returns_trimmed_detail(tmp_path, monkeypatch):
    sc, _ = client_for(tmp_path)
    rec = patch_http(monkeypatch, Recorder(get=Resp(200, raw_detail())))
    d = sc.fetch_activity(5)
    assert d["polyline"] == GOOGLE_EXAMPLE and "photos" not in d
    assert rec.gets[0][0].endswith("/activities/5") and rec.gets[0][1]["Authorization"] == "Bearer t1"


@pytest.mark.parametrize("code,expect", [(404, "no details"), (403, "no details"), (429, MSG_RATE)])
def test_fetch_activity_errors_are_clear(tmp_path, monkeypatch, code, expect):
    sc, _ = client_for(tmp_path)
    patch_http(monkeypatch, Recorder(get=Resp(code)))
    with pytest.raises(StravaError, match=expect if code != 429 else "rate limit"):
        sc.fetch_activity(5)
    assert sc.connected


def test_fetch_activity_revoked_and_network(tmp_path, monkeypatch):
    sc, udb = client_for(tmp_path)
    patch_http(monkeypatch, Recorder(get=Resp(401), post=Resp(400)))
    with pytest.raises(StravaError) as e:
        sc.fetch_activity(5)
    assert str(e.value) == MSG_REVOKED and udb.kv_get("strava_token") is None
    sc2, _ = client_for(tmp_path / "n")
    patch_http(monkeypatch, Recorder(get=httpx.ConnectError("down")))
    with pytest.raises(StravaError) as e2:
        sc2.fetch_activity(5)
    assert str(e2.value) == MSG_NETWORK


# ------------------------------------------------------------------------------------------- the view model ---
def act(**kw):
    base = {"id": 1, "date": "2026-11-03", "start_local": "2026-11-03T07:05:00Z", "name": "Run", "distance_m": 8000.0, "moving_s": 2880,
            "avg_hr": 150.0, "elevation_m": 40.0}
    base.update(kw)
    return base


def splits(*paces, last=None):
    out = [{"split": i + 1, "distance": 1000.0, "moving_time": p, "elapsed_time": p, "average_heartrate": 140 + i, "elevation_difference": 0.0}
           for i, p in enumerate(paces)]
    if last:
        out.append({"split": len(out) + 1, "distance": 400.0, "moving_time": last, "elapsed_time": last})
    return out


def test_splits_get_bars_fastest_slowest_and_partial_flags():
    v = build_run_view(act(), None, {"splits": splits(360, 340, 380, 350, last=140), "polyline": ""}, None)
    s = v["splits"]
    assert [round(x["pace"]) for x in s] == [360, 340, 380, 350, 350]
    assert s[1]["fastest"] and s[2]["slowest"] and not s[4]["fastest"]          # the 400 m remainder can't be "fastest"
    assert s[4]["partial"] and not s[0]["partial"]
    assert s[1]["bar"] == 100 and s[2]["bar"] == 55 and 55 < s[0]["bar"] < 100
    assert v["start_time"] == "07:05" and v["pace_s_km"] == pytest.approx(360)
    assert v["route"] is None and v["has_detail"]


def test_even_splits_do_not_crown_a_fastest_or_slowest():
    v = build_run_view(act(), None, {"splits": splits(350, 350, 350)}, None)
    assert all(s["bar"] == 100 for s in v["splits"])
    assert not any(s["fastest"] or s["slowest"] for s in v["splits"])


def test_plan_comparison_verdicts():
    w = {"type": "easy", "title": "Easy run", "distance_km": 8.0, "duration_min": 50, "pace_guidance": "RPE 3-4"}
    assert build_run_view(act(distance_m=8200), w, None, None)["plan"]["verdict"] == "Right on plan."
    assert "25% longer" in build_run_view(act(distance_m=10000), w, None, None)["plan"]["verdict"]
    assert "50% shorter" in build_run_view(act(distance_m=4000), w, None, None)["plan"]["verdict"]
    assert build_run_view(act(), {**w, "distance_km": None}, None, None)["plan"]["verdict"] == ""
    assert build_run_view(act(), None, None, None)["plan"] is None


def test_easy_runs_that_were_too_fast_get_a_coaching_note_and_band_scoring():
    paces = {"easy": (300, 360), "threshold": (255, 255), "interval": (235, 235)}
    w = {"type": "easy", "title": "Easy run", "distance_km": 8.0, "duration_min": 50, "pace_guidance": ""}
    fast_run = act(moving_s=8 * 285)                                             # 4:45/km, faster than the 5:00-6:00 easy range
    v = build_run_view(fast_run, w, {"splits": splits(285, 285, 285, 285)}, paces)
    assert any("quicker than your easy range" in n for n in v["plan"]["notes"])
    assert v["band"] == (292, 368) and all(s["in_band"] is False for s in v["splits"])
    assert any("0 of 4 splits" in n for n in v["plan"]["notes"])
    ok = build_run_view(act(moving_s=8 * 330), w, {"splits": splits(330, 332, 328, 331)}, paces)
    assert not any("quicker" in n for n in ok["plan"]["notes"]) and all(s["in_band"] for s in ok["splits"])
    tempo = build_run_view(act(), {**w, "type": "tempo"}, {"splits": splits(256, 254)}, paces)
    assert tempo["band"] == (247, 263) and all(s["in_band"] for s in tempo["splits"])
    assert build_run_view(act(), {**w, "type": "race"}, {"splits": splits(300)}, paces)["band"] is None   # no zone for a race


def test_route_is_drawn_from_the_polyline():
    v = build_run_view(act(), None, {"polyline": GOOGLE_EXAMPLE}, None)
    assert v["route"] and v["route"]["d"].startswith("M") and v["route"]["start"] != v["route"]["end"]


# -------------------------------------------------------------------------------- service: fetch + cache ---
def add_run_with_workout(env, aid=950):
    w = next(x for x in env.udb.get_workouts(end=TODAY + timedelta(days=14)) if x["type"] == "easy")
    d = date.fromisoformat(w["date"])
    env.udb.upsert_activities([run_activity(aid, d, km=w["distance_km"], minutes=45)])
    env.udb.update_workout(w["id"], status="completed", activity_id=aid)
    return w


def test_service_fetches_detail_once_and_caches_it(tmp_path):
    env = Env(tmp_path)
    w = add_run_with_workout(env)
    env.svc.strava.details[950] = trim_activity_detail(raw_detail())
    v1 = env.svc.run_detail(950)
    v2 = env.svc.run_detail(950)
    assert env.svc.strava.fetches == [950]                                          # the second view came from the cache
    assert v1["route"] and v1["splits"] and v2["splits"] == v1["splits"]
    assert v1["workout"]["id"] == w["id"] and v1["plan"] and v1["detail_note"] is None


def test_without_strava_or_when_strava_fails_the_page_still_works_with_a_reason(tmp_path):
    env = Env(tmp_path)
    add_run_with_workout(env)
    env.svc.strava.connected = False
    v = env.svc.run_detail(950)
    assert v["has_detail"] is False and v["route"] is None and "Connect Strava" in v["detail_note"] and v["km"] > 0
    env.svc.strava.connected = True
    env.svc.strava.fetch_error = StravaError(MSG_RATE)
    assert env.svc.run_detail(950)["detail_note"] == MSG_RATE
    assert env.udb.get_activity_detail(950) is None                                  # failures are not cached
    env.svc.strava.fetch_error = None
    assert env.svc.run_detail(951) is None                                           # unknown run


def test_another_users_run_is_invisible(tmp_path):
    env = Env(tmp_path)
    add_run_with_workout(env)
    other = env.db.create_user("b@example.com", "B", "x")
    svc2 = type(env.svc)(env.db.for_user(other), Settings(db_path=env.db.path), env.coach, FakeStrava(), clock=lambda: TODAY)
    assert svc2.run_detail(950) is None
    assert env.db.for_user(other).get_activity_detail(950) is None


def test_recent_runs_newest_first_with_feelings(tmp_path):
    env = Env(tmp_path)
    env.udb.upsert_activities([run_activity(10 + i, TODAY - timedelta(days=i + 1), km=5 + i) for i in range(15)])
    env.svc.save_feedback(10, "hard", 8)
    runs = env.svc.recent_runs()
    assert len(runs) == 12 and runs[0]["id"] == 10 and runs[0]["feeling"] == "hard" and runs[1]["feeling"] is None
    assert [r["date"] for r in runs] == sorted((r["date"] for r in runs), reverse=True)


# ------------------------------------------------------------------------------------------------- the page ---
def web(tmp_path):
    strava = FakeStrava()
    s = Settings(db_path=str(tmp_path / "w.db"), sync_interval_min=0)
    db = DB(s.db_path)
    app = create_app(s, db=db, coach=ReferenceCoach(), strava_factory=lambda u: strava, clock=lambda: TODAY)
    c = TestClient(app)
    signup(c)
    c.post("/setup", data={**SETUP, "baseline": "25", "goal_time": "20:00"})
    c.post("/plan/generate")
    assert wait_job(c)["status"] == "done"
    return c, db, strava


def link_run(db, aid=950):
    w = next(x for x in db.for_user(1).get_workouts(end=TODAY + timedelta(days=14)) if x["type"] == "easy")
    db.for_user(1).upsert_activities([run_activity(aid, date.fromisoformat(w["date"]), km=w["distance_km"], minutes=45)])
    db.for_user(1).update_workout(w["id"], status="completed", activity_id=aid)
    return w


def test_run_page_shows_stats_route_splits_plan_and_feedback(tmp_path):
    c, db, strava = web(tmp_path)
    w = link_run(db)
    strava.details[950] = trim_activity_detail(raw_detail(splits_metric=[
        {"split": i + 1, "distance": 1000.0, "moving_time": 330 + i, "elapsed_time": 330, "average_heartrate": 145 + i, "elevation_difference": 1.0} for i in range(5)]))
    page = c.get("/run/950").text
    assert 'class="route"' in page and "never sent anywhere" in page and "Splits" in page and "Planned vs actual" in page
    assert w["title"] in page and any(x in page for x in ("Right on plan", "longer than planned", "shorter than planned"))
    assert page.count('class="split') >= 5 and ">150<" in page.replace(" ", "")               # average heart rate (150 from the activity row)
    assert 'id="feedback"' in page and 'name="feeling"' in page
    assert strava.fetches == [950]
    c.get("/run/950")
    assert strava.fetches == [950]                                                             # cached for the second visit


def test_feedback_from_the_run_page_is_saved_and_prefilled(tmp_path):
    c, db, strava = web(tmp_path)
    link_run(db)
    c.post("/feedback", data={"activity_id": 950, "feeling": "hard", "rpe": "8", "note": "windy", "next": "/run/950"}, follow_redirects=True)
    page = c.get("/run/950").text
    assert 'value="hard"' in page and "checked" in page and 'value="windy"' in page and ">Update<" in page
    assert db.for_user(1).get_feedback(950)["rpe"] == 8


def test_run_page_degrades_gracefully_and_hides_other_users_runs(tmp_path):
    c, db, strava = web(tmp_path)
    link_run(db)
    strava.connected = False
    page = c.get("/run/950").text
    assert "Connect Strava to see this run" in page and 'class="route"' not in page and "Planned vs actual" in page
    r = c.get("/run/424242", follow_redirects=True)
    assert "That run wasn&#39;t found" in r.text
    b = TestClient(c.app)
    signup(b, "b@example.com", "B")
    b.post("/setup", data={**SETUP, "baseline": "25"})
    assert "That run wasn&#39;t found" in b.get("/run/950", follow_redirects=True).text


def test_run_links_appear_on_plan_progress_and_today(tmp_path):
    c, db, _ = web(tmp_path)
    link_run(db)
    assert 'href="/run/950"' in c.get("/plan").text
    assert 'href="/run/950"' in c.get("/progress").text and "Recent runs" in c.get("/progress").text
    db.for_user(1).upsert_activities([run_activity(960, TODAY, km=6)])
    w = next(x for x in db.for_user(1).get_workouts(TODAY, TODAY) if x["status"] == "planned") if db.for_user(1).get_workouts(TODAY, TODAY) else None
    if w:
        db.for_user(1).update_workout(w["id"], status="completed", activity_id=960)
        assert 'href="/run/960"' in c.get("/").text


def test_account_deletion_removes_cached_run_details(tmp_path):
    c, db, strava = web(tmp_path)
    link_run(db)
    strava.details[950] = trim_activity_detail(raw_detail())
    c.get("/run/950")
    assert db.for_user(1).get_activity_detail(950)
    c.post("/account/delete", data={"confirm": "a@example.com"})
    assert db.for_user(1).get_activity_detail(950) is None and db.for_user(1).feedback_map() == {}
