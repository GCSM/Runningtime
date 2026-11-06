"""Calendar feed: RFC 5545 correctness (checked with an independent mini-parser), tokens, privacy."""
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from coach import ics
from coach.config import Settings
from coach.db import DB
from coach.web import create_app
from test_actions import Env, TODAY
from test_core import FakeStrava, signup, wait_job
from test_scenarios import ReferenceCoach, SETUP, prof

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------------------ a deliberately separate parser ---
def unfold(text: str) -> list[str]:
    assert "\r\n" in text and "\n" not in text.replace("\r\n", ""), "lines must end in CRLF, with no bare LF"
    return re.sub(r"\r\n[ \t]", "", text).split("\r\n")[:-1]          # drop the empty string after the final CRLF


def parse(text: str) -> dict:
    """-> {'props': {...}, 'events': [{'props': {name: (params, value)}, 'alarms': [...]}, ...]}"""
    root, stack = {"props": {}, "events": []}, []
    cur = None
    for line in unfold(text):
        name, _, value = line.partition(":")
        key, *params = name.split(";")
        if key == "BEGIN":
            comp = {"type": value, "props": {}, "alarms": []}
            stack.append(comp)
            continue
        if key == "END":
            comp = stack.pop()
            assert comp["type"] == value, f"mismatched END:{value} (open: {comp['type']})"
            if value == "VEVENT":
                root["events"].append(comp)
            elif value == "VALARM":
                stack[-1]["alarms"].append(comp)
            continue
        target = stack[-1]["props"] if stack and stack[-1]["type"] != "VCALENDAR" else root["props"]
        target[key] = (dict(p.split("=", 1) for p in params), value)
    assert not stack, "unclosed component"
    return root


def unescape(v: str) -> str:
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v)


def w(i, d, typ="easy", status="planned", title="Easy run", km=8.0, dur=50, desc="Conversational.", pace="RPE 3-4"):
    return {"id": i, "date": d.isoformat(), "week_start": (d - timedelta(days=d.weekday())).isoformat(), "type": typ, "status": status,
            "title": title, "distance_km": km, "duration_min": dur, "description": desc, "pace_guidance": pace}


AV = {k: {"available": True, "minutes": 60, "time": ""} for k in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


def feed(workouts, av=None, units="km"):
    return ics.build_calendar(workouts, availability=av if av is not None else AV, units=units, user_id=7, base_url="http://coach.lan", now=NOW)


# ================================================================================================ text rules ====
@pytest.mark.parametrize("raw,escaped", [("a,b", "a\\,b"), ("a;b", "a\\;b"), ("back\\slash", "back\\\\slash"), ("line1\nline2", "line1\\nline2"),
                                         ("crlf\r\nx", "crlf\\nx"), ("plain", "plain"), ("", "")])
def test_text_escaping_roundtrips(raw, escaped):
    assert ics.escape_text(raw) == escaped
    assert unescape(escaped) == raw.replace("\r\n", "\n")


def test_folding_never_exceeds_75_octets_never_splits_characters_and_unfolds_back():
    for line in ("X" * 200, "SUMMARY:" + "é" * 120, "DESCRIPTION:" + "🏃" * 60 + " and some text, with; punctuation", "short", "A" * 75, "A" * 76, "é" * 37 + "A"):
        folded = ics.fold(line)
        parts = folded.split("\r\n")
        assert all(len(p.encode("utf-8")) <= 75 for p in parts), line[:20]
        assert all(p.startswith(" ") for p in parts[1:])
        assert re.sub(r"\r\n ", "", folded) == line                                   # unfolding restores the original exactly
        for p in parts:
            p.encode("utf-8").decode("utf-8")                                         # every physical line is valid UTF-8 by itself


@pytest.mark.parametrize("text,expect", [("06:30", (6, 30)), ("6:30am", (6, 30)), ("6:30 PM", (18, 30)), ("18:05", (18, 5)), ("12:00am", (0, 0)),
                                         ("12:15pm", (12, 15)), (" 07:00 ", (7, 0)), ("", None), ("soon", None), ("25:00", None), ("13:00pm", None),
                                         ("0:30pm", None), ("7:61", None), (None, None)])
def test_usual_time_parsing(text, expect):
    assert ics.parse_time(text) == expect


# ============================================================================================ feed structure ====
def test_calendar_envelope_and_required_properties():
    cal = parse(feed([w(1, TODAY)]))
    assert cal["props"]["VERSION"][1] == "2.0" and cal["props"]["PRODID"][1].startswith("-//RunningTime")
    assert cal["props"]["METHOD"][1] == "PUBLISH" and cal["props"]["X-WR-CALNAME"][1] == "RunningTime"
    assert cal["props"]["REFRESH-INTERVAL"][0] == {"VALUE": "DURATION"} and cal["props"]["REFRESH-INTERVAL"][1] == "PT6H"
    ev = cal["events"][0]["props"]
    for required in ("UID", "DTSTAMP", "DTSTART", "SUMMARY"):
        assert required in ev
    assert ev["DTSTAMP"][1] == "20261104T120000Z" and ev["TRANSP"][1] == "TRANSPARENT"


def test_all_day_events_when_there_is_no_usual_time():
    ev = parse(feed([w(1, TODAY + timedelta(days=1))]))["events"][0]
    assert ev["props"]["DTSTART"] == ({"VALUE": "DATE"}, "20261105") and ev["props"]["DTEND"] == ({"VALUE": "DATE"}, "20261106")
    alarm = ev["alarms"][0]["props"]
    assert alarm["TRIGGER"] == ({"RELATED": "START"}, "PT7H") and alarm["ACTION"][1] == "DISPLAY"


def test_timed_events_use_the_usual_time_for_that_weekday_and_the_duration():
    av = {**AV, "thu": {"available": True, "minutes": 60, "time": "6:30am"}}          # 2026-11-05 is a Thursday
    ev = parse(feed([w(1, date(2026, 11, 5), dur=50), w(2, date(2026, 11, 6), dur=40)], av))["events"]
    thu, fri = ev[0]["props"], ev[1]["props"]
    assert thu["DTSTART"] == ({}, "20261105T063000") and thu["DTEND"] == ({}, "20261105T072000")      # floating local time, 50 min
    assert ev[0]["alarms"][0]["props"]["TRIGGER"][1] == "-PT30M"
    assert fri["DTSTART"][0] == {"VALUE": "DATE"}                                                      # no time set for Friday: all-day
    assert "Z" not in thu["DTSTART"][1] and "TZID" not in str(thu["DTSTART"][0])


def test_a_timed_event_without_a_duration_gets_a_sensible_default_and_can_cross_midnight():
    av = {**AV, "thu": {"available": True, "minutes": 60, "time": "23:50"}}
    ev = parse(feed([w(1, date(2026, 11, 5), dur=None)], av))["events"][0]["props"]
    assert ev["DTSTART"][1] == "20261105T235000" and ev["DTEND"][1] == "20261106T003500"              # +45 min default


def test_skipped_workouts_are_omitted_and_completed_ones_are_marked():
    cal = parse(feed([w(1, TODAY, title="Done run", status="completed"), w(2, TODAY + timedelta(days=1), title="Skipped", status="skipped"),
                      w(3, TODAY + timedelta(days=2), title="Later", status="planned"), w(4, TODAY - timedelta(days=1), title="Half", status="partial")]))
    titles = [unescape(e["props"]["SUMMARY"][1]) for e in cal["events"]]
    assert len(titles) == 3 and not any("Skipped" in t for t in titles)
    assert any(t.startswith("✅") and "Done run" in t for t in titles)
    assert any(t.startswith("✅") and "Half" in t for t in titles) and any(t.startswith("🏃") and "Later" in t for t in titles)


def test_summary_shows_distance_in_the_users_units_and_race_gets_a_flag():
    km = unescape(parse(feed([w(1, TODAY, km=10.0)]))["events"][0]["props"]["SUMMARY"][1])
    mi = unescape(parse(feed([w(1, TODAY, km=10.0)], units="mi"))["events"][0]["props"]["SUMMARY"][1])
    assert "(10.0 km)" in km and "(6.2 mi)" in mi
    race = unescape(parse(feed([w(1, TODAY, typ="race", title="Race day: 10K", km=10.0)]))["events"][0]["props"]["SUMMARY"][1])
    assert race.startswith("🏁")
    assert "(" not in unescape(parse(feed([w(1, TODAY, km=0)]))["events"][0]["props"]["SUMMARY"][1])


def test_description_carries_pace_duration_link_and_survives_special_characters():
    nasty = "Warm up; 4 x 1.5 km, steady\nCool down \\ done"
    ev = parse(feed([w(1, TODAY, desc=nasty, pace="5:00/km, RPE 7; hard", title="Tempo, \"4 x 1.5\"; hills")]))["events"][0]["props"]
    d = unescape(ev["DESCRIPTION"][1])
    assert "Warm up; 4 x 1.5 km, steady\nCool down \\ done" in d and "Pace: 5:00/km, RPE 7; hard" in d and "About 50 min." in d
    assert d.rstrip().endswith("Open RunningTime: http://coach.lan/")
    assert unescape(ev["SUMMARY"][1]).split(" ", 1)[1].startswith('Tempo, "4 x 1.5"; hills')


def test_uids_are_stable_across_rebuilds_unique_and_scoped_to_the_user():
    a = [e["props"]["UID"][1] for e in parse(feed([w(1, TODAY), w(2, TODAY + timedelta(days=1))]))["events"]]
    b = [e["props"]["UID"][1] for e in parse(feed([w(1, TODAY + timedelta(days=5)), w(2, TODAY + timedelta(days=1))]))["events"]]
    assert len(set(a)) == 2 and a[0] == "rc-7-w1@coach.lan"
    assert "rc-7-w1@coach.lan" in b                                                    # moving a workout keeps its UID (so calendars update it, not duplicate it)
    other = ics.build_calendar([w(1, TODAY)], availability=AV, units="km", user_id=8, base_url="http://coach.lan", now=NOW)
    assert "rc-8-w1@coach.lan" in other and "rc-7-w1" not in other


def test_events_are_in_date_order_and_empty_plan_is_a_valid_empty_calendar():
    cal = parse(feed([w(2, TODAY + timedelta(days=3)), w(1, TODAY)]))
    assert [e["props"]["DTSTART"][1] for e in cal["events"]] == ["20261104", "20261107"]
    empty = parse(feed([]))
    assert empty["events"] == [] and empty["props"]["VERSION"][1] == "2.0"


def test_every_physical_line_is_within_75_octets_even_with_long_unicode_text():
    long_desc = "Run easy 🏃 " * 60
    text = feed([w(i, TODAY + timedelta(days=i), title="É" * 90, desc=long_desc) for i in range(5)])
    raw_lines = text.split("\r\n")
    assert all(len(l.encode("utf-8")) <= 75 for l in raw_lines)
    assert len(parse(text)["events"]) == 5


# ==================================================================================== service + web: tokens ====
def web(tmp_path):
    strava = FakeStrava()
    s = Settings(db_path=str(tmp_path / "w.db"), sync_interval_min=0)
    db = DB(s.db_path)
    app = create_app(s, db=db, coach=ReferenceCoach(), strava_factory=lambda u: strava, clock=lambda: TODAY)
    c = TestClient(app)
    signup(c)
    c.post("/setup", data={**SETUP, "baseline": "25", "time_tue": "06:30", "time_sun": "8:00am"})
    c.post("/plan/generate")
    assert wait_job(c)["status"] == "done"
    return c, db


def feed_url(c):
    page = c.get("/me").text
    m = re.search(r'id="cal-url" value="([^"]+)"', page)
    return m.group(1) if m else None


def test_no_feed_exists_until_the_user_turns_it_on(tmp_path):
    c, db = web(tmp_path)
    assert feed_url(c) is None and "Create my calendar link" in c.get("/me").text
    assert db.for_user(1).kv_get("ics_token") is None


def test_enabling_creates_an_unguessable_link_that_works_without_a_login(tmp_path):
    c, db = web(tmp_path)
    c.post("/calendar/enable")
    url = feed_url(c)
    token = db.for_user(1).kv_get("ics_token")
    assert url.endswith(f"/cal/{token}.ics") and len(token) >= 40 and re.fullmatch(r"[A-Za-z0-9_-]+", token)
    anon = TestClient(c.app)                                                             # no cookies at all
    r = anon.get(f"/cal/{token}.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar") and "private" in r.headers["cache-control"]
    cal = parse(r.text)
    assert len(cal["events"]) > 10 and cal["events"][-1]["props"]["SUMMARY"][1].startswith("🏁")                 # the race is the last event
    assert any(e["props"]["DTSTART"][1].endswith("T063000") for e in cal["events"])                               # Tuesdays at the usual 06:30
    assert any(e["props"]["DTSTART"][1].endswith("T080000") for e in cal["events"])                               # Sundays "8:00am" -> 08:00
    assert "webcal://" in c.get("/me").text


def test_bad_tokens_get_a_plain_404_and_reveal_nothing(tmp_path):
    c, db = web(tmp_path)
    c.post("/calendar/enable")
    anon = TestClient(c.app)
    real = db.for_user(1).kv_get("ics_token")
    for bad in ("x" * 30, real[:-1] + ("A" if real[-1] != "A" else "B"), "short", real + "extra", "%00" * 25, "..%2F..%2Fetc%2Fpasswd"):
        r = anon.get(f"/cal/{bad}.ics")
        assert r.status_code == 404 and "BEGIN:VCALENDAR" not in r.text, bad
    # '../..' is normalised away by HTTP clients before it is sent, so it lands outside /cal/ and is stopped by the login gate
    r = anon.get("/cal/../../etc/passwd.ics", follow_redirects=False)
    assert r.status_code in (303, 404) and "BEGIN:VCALENDAR" not in r.text
    assert anon.get("/cal/.ics").status_code == 404 and anon.get(f"/cal/{real}").status_code in (404, 405)       # must end in .ics


def test_a_new_link_invalidates_the_old_one_and_turning_off_removes_it(tmp_path):
    c, db = web(tmp_path)
    c.post("/calendar/enable")
    old = db.for_user(1).kv_get("ics_token")
    c.post("/calendar/enable")
    new = db.for_user(1).kv_get("ics_token")
    anon = TestClient(c.app)
    assert old != new and anon.get(f"/cal/{old}.ics").status_code == 404 and anon.get(f"/cal/{new}.ics").status_code == 200
    c.post("/calendar/disable")
    assert db.for_user(1).kv_get("ics_token") is None and anon.get(f"/cal/{new}.ics").status_code == 404


def test_the_feed_only_ever_contains_the_owners_workouts(tmp_path):
    c, db = web(tmp_path)
    c.post("/calendar/enable")
    b = TestClient(c.app)
    signup(b, "b@example.com", "B")
    b.post("/setup", data={**SETUP, "baseline": "25", "goal_preset": "10K"})
    b.post("/plan/generate")
    wait_job(b)
    b.post("/calendar/enable")
    ta, tb = db.for_user(1).kv_get("ics_token"), db.for_user(2).kv_get("ics_token")
    anon = TestClient(c.app)
    ua = {e["props"]["UID"][1] for e in parse(anon.get(f"/cal/{ta}.ics").text)["events"]}
    ub = {e["props"]["UID"][1] for e in parse(anon.get(f"/cal/{tb}.ics").text)["events"]}
    assert ua and ub and all("rc-1-" in u for u in ua) and all("rc-2-" in u for u in ub) and not (ua & ub)


def test_the_feed_follows_plan_changes(tmp_path):
    c, db = web(tmp_path)
    c.post("/calendar/enable")
    token = db.for_user(1).kv_get("ics_token")
    anon = TestClient(c.app)
    before = parse(anon.get(f"/cal/{token}.ics").text)["events"]
    w = next(x for x in db.for_user(1).get_workouts(TODAY + timedelta(days=1)) if x["type"] == "easy")
    c.post(f"/workout/{w['id']}/skip", data={"next": "/"})
    after = parse(anon.get(f"/cal/{token}.ics").text)["events"]
    assert len(after) == len(before) - 1 and f"rc-1-w{w['id']}@testserver" not in {e["props"]["UID"][1] for e in after}


def test_deleting_the_account_kills_the_link(tmp_path):
    c, db = web(tmp_path)
    c.post("/calendar/enable")
    token = db.for_user(1).kv_get("ics_token")
    c.post("/account/delete", data={"confirm": "a@example.com"})
    assert TestClient(c.app).get(f"/cal/{token}.ics").status_code == 404


def test_other_pages_still_require_a_login(tmp_path):
    c, _ = web(tmp_path)
    anon = TestClient(c.app)
    assert anon.get("/", follow_redirects=False).status_code == 303 and anon.get("/calendar/enable", follow_redirects=False).status_code in (303, 405)
    assert anon.post("/calendar/enable", follow_redirects=False).status_code == 303


def test_feed_is_accepted_by_an_independent_icalendar_parser():
    icalendar = pytest.importorskip("icalendar")          # a real third-party parser, like the one inside a phone's calendar
    av = {**AV, "thu": {"available": True, "minutes": 60, "time": "6:30am"}}
    text = feed([w(1, date(2026, 11, 5), title='Tempo, "4 x 1.5"; hills', desc="Warm up; 4 x 1.5 km, steady\nCool down", dur=50),
                 w(2, date(2026, 11, 7), typ="long", title="Long run 🏃 " + "é" * 80, km=16.0, dur=100),
                 w(3, date(2026, 11, 8), typ="race", title="Race day: 10K", km=10.0)], av)
    cal = icalendar.Calendar.from_ical(text)
    events = cal.walk("VEVENT")
    assert len(events) == 3 and str(cal["X-WR-CALNAME"]) == "RunningTime"
    first = events[0]
    assert first["DTSTART"].dt == datetime(2026, 11, 5, 6, 30) and first["DTEND"].dt == datetime(2026, 11, 5, 7, 20)
    assert str(first["SUMMARY"]).endswith('Tempo, "4 x 1.5"; hills (8.0 km)') and str(first["DESCRIPTION"]).startswith("Warm up; 4 x 1.5 km, steady\nCool down")
    assert events[1]["DTSTART"].dt == date(2026, 11, 7) and "é" * 80 in str(events[1]["SUMMARY"])          # a folded, multi-byte title survived
    assert first.walk("VALARM")[0]["TRIGGER"].dt == timedelta(minutes=-30) and events[2].walk("VALARM")[0]["TRIGGER"].dt == timedelta(hours=7)
