"""Coach chat: cheapest model and bounded cost, proposals validated and applied only on confirmation."""
import json
import os
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from coach import knowledge as K
from coach.chat import HISTORY_MESSAGES, MAX_ACTIONS, MAX_TOKENS, MAX_USER_CHARS, SYSTEM, TOOL, ChatCoach, ChatService
from coach.config import Settings
from coach.db import DB
from coach.errors import CoachUnavailable, UserError
from coach.web import create_app
from test_actions import Env, TODAY, free_usual_day
from test_core import FakeStrava, signup, wait_job
from test_scenarios import ReferenceCoach, SETUP, run_activity


# ----------------------------------------------------------------------------------------------------- doubles ---
class FakeChat:
    """Stands in for the model: returns a canned reply (and optional proposal), records what it was given."""
    def __init__(self, reply="Sounds good.", proposal=None, raises=None):
        self.reply_text, self.proposal, self.raises, self.calls = reply, proposal, raises, []

    def reply(self, context, history, text):
        self.calls.append({"context": context, "history": history, "text": text})
        if self.raises:
            raise self.raises
        return {"reply": self.reply_text, "proposal": self.proposal}


class FakeSDK:
    def __init__(self, content, stop="end_turn"):
        self.kwargs = None
        self.messages = NS(create=self._create)
        self._resp = NS(content=content, stop_reason=stop)

    def _create(self, **kw):
        self.kwargs = kw
        return self._resp


def text(t):
    return NS(type="text", text=t)


def tool(**inp):
    return NS(type="tool_use", name="propose_changes", input=inp)


def service(env, llm=None, limit=40):
    return ChatService(env.svc, llm or FakeChat(), limit)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ------------------------------------------------------------------------ cost: the model call is small and cheap ---
def test_defaults_use_the_cheapest_model_at_low_effort_with_a_daily_cap():
    s = Settings()
    assert (s.chat_model, s.chat_effort, s.chat_daily_limit) == ("claude-haiku-5-5", "low", 40)
    old = {k: os.environ.get(k) for k in ("COACH_CHAT_MODEL", "COACH_CHAT_EFFORT", "CHAT_MESSAGES_PER_DAY")}
    os.environ.update(COACH_CHAT_MODEL="m-x", COACH_CHAT_EFFORT="medium", CHAT_MESSAGES_PER_DAY="12")
    try:
        e = Settings.from_env()
        assert (e.chat_model, e.chat_effort, e.chat_daily_limit) == ("m-x", "medium", 12)
    finally:
        for k, v in old.items():
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
    assert Settings().model != Settings().chat_model                                  # planning and chat use different models


def test_request_shape_is_valid_cheap_and_never_forces_a_tool():
    sdk = FakeSDK([text("Hi there")])
    out = ChatCoach("claude-haiku-5-5", "low", client=sdk).reply({"today": "2026-11-04"}, [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}], "next question")
    kw = sdk.kwargs
    assert out == {"reply": "Hi there", "proposal": None}
    assert kw["model"] == "claude-haiku-5-5" and kw["output_config"] == {"effort": "low"} and kw["max_tokens"] == MAX_TOKENS <= 700
    assert kw["tool_choice"] == {"type": "auto"} and kw["tools"] == [TOOL] and "temperature" not in kw
    assert [m["role"] for m in kw["messages"]] == ["user", "assistant", "user"] and kw["messages"][-1]["content"] == "next question"
    assert kw["system"][0]["text"] == SYSTEM and kw["system"][1]["text"].startswith("Context (JSON):") and '"today":"2026-11-04"' in kw["system"][1]["text"]


def test_reply_parsing_covers_text_tool_truncation_refusal_and_empty():
    c = lambda content, stop="end_turn": ChatCoach("m", client=FakeSDK(content, stop)).reply({}, [], "x")  # noqa: E731
    prop = {"summary": "Ease off", "actions": [{"action": "ease_off"}]}
    both = c([text("I'd rest."), tool(**prop)])
    assert both["reply"] == "I'd rest." and both["proposal"] == prop
    assert c([tool(**prop)])["reply"] == "Ease off"                                     # tool-only answer still shows something
    assert c([text("Long answer cut")], "max_tokens")["reply"] == "Long answer cut…"
    assert "can't help" in c([], "refusal")["reply"]
    assert "rephrase" in c([])["reply"]


def test_missing_api_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(CoachUnavailable, match="No Anthropic API key"):
        ChatCoach("claude-haiku-5-5").reply({}, [], "hi")


def test_tool_schema_is_closed_to_the_actions_we_can_perform():
    items = TOOL["input_schema"]["properties"]["actions"]
    assert items["maxItems"] == MAX_ACTIONS == 3 and set(items["items"]["properties"]["action"]["enum"]) == {"skip", "move", "swap", "ease_off", "replan"}
    assert "race" in SYSTEM.lower() and "confirm" in SYSTEM.lower() and "diagnose" in SYSTEM.lower()


# ------------------------------------------------------------------------------------ context and history size ---
def test_context_is_compact_useful_and_leaks_nothing_private(env):
    env.udb.upsert_activities([run_activity(1, TODAY - timedelta(days=2), km=8)])
    env.svc.save_feedback(1, "hard", 8)
    env.svc.ease_off()
    ctx = service(env).context()
    blob = json.dumps(ctx)
    assert len(blob) < 5000                                                              # ≈1.2k tokens: it is paid for on every message
    assert ctx["athlete"]["goal"] == "10K" and ctx["athlete"]["units"] == "km" and ctx["athlete"]["days_to_race"] > 0
    assert ctx["next_10_days"] and all({"id", "date", "type", "title", "status"} <= set(w) for w in ctx["next_10_days"])
    assert ctx["recent_runs_7d"][0]["felt"] == "hard" and ctx["eased_off_until"]
    assert "a@example.com" not in blob and "pw_hash" not in blob and "token" not in blob.lower() and "api" not in blob.lower()
    assert all(TODAY.isoformat() <= w["date"] <= (TODAY + timedelta(days=10)).isoformat() for w in ctx["next_10_days"])


def test_training_paces_are_included_when_known(env):
    env.udb.kv_set("volume_outlook", {"paces_s": {"easy": [300, 360], "marathon": [270, 270], "threshold": [255, 255], "interval": [235, 235], "repetition": [220, 220]}})
    assert service(env).context()["training_paces"]["threshold"] == "4:15/km"


def test_history_is_trimmed_alternating_starts_on_a_user_turn_and_never_ends_on_one(env):
    for i in range(7):
        env.udb.add_chat_message("user", f"q{i}")
        env.udb.add_chat_message("assistant", f"a{i}")
    h = service(env).history()
    assert len(h) <= HISTORY_MESSAGES and h[0]["role"] == "user" and h[-1]["role"] == "assistant"
    assert all(a["role"] != b["role"] for a, b in zip(h, h[1:]))
    env.udb.clear_chat()
    env.udb.add_chat_message("assistant", "orphan")
    env.udb.add_chat_message("user", "a")
    env.udb.add_chat_message("user", "b")                                                # two user turns in a row (earlier reply failed)
    assert service(env).history() == []                                                  # …a dangling user turn is dropped so the new one doesn't collide
    env.udb.add_chat_message("assistant", "A" * 5000)
    assert len(service(env).history()[0]["content"]) <= MAX_USER_CHARS * 2 + 10


# ------------------------------------------------------------------------------------------- sending and limits ---
def test_send_persists_both_sides_and_counts_down_the_allowance(env):
    cs = service(env, FakeChat("Take it easy."), limit=5)
    out = cs.send("  how am I doing?  ")
    assert out["reply"] == "Take it easy." and out["proposal"] is None and out["remaining"] == 4
    assert [(m["role"], m["content"]) for m in env.udb.recent_chat()] == [("user", "how am I doing?"), ("assistant", "Take it easy.")]
    assert cs.llm.calls[0]["text"] == "how am I doing?" and cs.llm.calls[0]["history"] == []
    cs.send("again")
    assert cs.llm.calls[1]["history"][0]["content"] == "how am I doing?"                  # the model sees the earlier turn


def test_input_validation_and_the_daily_limit(env):
    cs = service(env, limit=2)
    with pytest.raises(UserError, match="Type a message"):
        cs.send("   ")
    with pytest.raises(UserError, match="under 600"):
        cs.send("x" * (MAX_USER_CHARS + 1))
    cs.send("one")
    cs.send("two")
    with pytest.raises(UserError, match="chat messages"):
        cs.send("three")
    assert len(cs.llm.calls) == 2 and cs.used_today() == 2                                # the blocked message never reached the model
    other = service(env, limit=2)                                                         # a failed call doesn't count either
    other.llm.raises = RuntimeError("boom")
    # rolling window: messages older than 24h stop counting
    with sqlite3.connect(env.db.path) as c:
        c.execute("update chat_messages set created_at='2020-01-01T00:00:00+00:00' where user_id=?", (env.uid,))
    assert cs.used_today() == 0
    cs.send("fresh start")


def test_a_failed_model_call_saves_nothing_and_costs_no_allowance(env):
    cs = service(env, FakeChat(raises=RuntimeError("down")), limit=3)
    with pytest.raises(RuntimeError):
        cs.send("hello")
    assert env.udb.recent_chat() == [] and cs.used_today() == 0


def test_allowance_is_per_user(env):
    service(env, limit=1).send("hi")
    other = env.db.create_user("b@example.com", "B", "x")
    from coach.service import CoachService
    svc2 = CoachService(env.db.for_user(other), Settings(db_path=env.db.path), env.coach, FakeStrava(), clock=lambda: TODAY)
    svc2.db.save_profile(env.udb.get_profile())
    cs2 = ChatService(svc2, FakeChat(), 1)
    assert cs2.used_today() == 0 and cs2.send("hi")["remaining"] == 0
    assert env.db.for_user(other).recent_chat()[0]["content"] == "hi" and len(env.udb.recent_chat()) == 2


# ----------------------------------------------------------------------------------------------- proposals ------
def two_easy(env):
    ws = [w for w in env.udb.get_workouts(TODAY + timedelta(days=1)) if w["type"] == "easy" and w["status"] == "planned"]
    return ws[0], ws[1]


def test_valid_actions_are_kept_with_readable_labels(env):
    a, b = two_easy(env)
    target = free_usual_day(env, after_days=4)
    raw = {"summary": "Rearrange", "actions": [{"action": "skip", "workout_id": a["id"]}, {"action": "move", "workout_id": b["id"], "to_date": target.isoformat()},
                                              {"action": "ease_off"}]}
    p = service(env).sanitize_proposal(raw)
    assert [x["action"] for x in p["actions"]] == ["skip", "move", "ease_off"] and p["summary"] == "Rearrange"
    assert a["title"] in p["actions"][0]["label"] and target.strftime("%b %-d") in p["actions"][1]["label"] and "7 days" in p["actions"][2]["label"]
    sw = service(env).sanitize_proposal({"actions": [{"action": "swap", "workout_id": a["id"], "other_workout_id": b["id"]}]})
    assert "Swap" in sw["actions"][0]["label"]
    rp = service(env).sanitize_proposal({"actions": [{"action": "replan", "note": "n" * 500}]})
    assert len(rp["actions"][0]["note"]) == 300


def test_invalid_or_dangerous_actions_are_dropped_not_trusted(env):
    a, b = two_easy(env)
    race = env.udb.get_workouts()[-1]
    done = next(w for w in env.udb.get_workouts(TODAY + timedelta(days=1)) if w["id"] not in (a["id"], b["id"]) and w["type"] != "race")
    env.udb.update_workout(done["id"], status="completed")
    other_user = env.db.create_user("b@example.com", "B", "x")
    env.db.for_user(other_user).save_profile(env.udb.get_profile())
    cs = service(env)
    bad = [
        {"action": "skip", "workout_id": race["id"]},                                    # the race
        {"action": "skip", "workout_id": done["id"]},                                    # already done
        {"action": "skip", "workout_id": 999999},                                        # doesn't exist
        {"action": "skip", "workout_id": "abc"}, {"action": "skip"},                     # malformed
        {"action": "move", "workout_id": a["id"], "to_date": (TODAY - timedelta(days=2)).isoformat()},    # the past
        {"action": "move", "workout_id": a["id"], "to_date": race["date"]},              # race day
        {"action": "move", "workout_id": a["id"], "to_date": "tomorrow-ish"},
        {"action": "swap", "workout_id": a["id"], "other_workout_id": a["id"]},          # with itself
        {"action": "swap", "workout_id": a["id"], "other_workout_id": race["id"]},
        {"action": "delete_everything"}, {"action": 5}, "skip", None,                    # not in the vocabulary / wrong type
    ]
    assert cs.sanitize_proposal({"summary": "x", "actions": bad}) is None
    assert cs.sanitize_proposal("rm -rf") is None and cs.sanitize_proposal(None) is None and cs.sanitize_proposal({"actions": "skip"}) is None
    mixed = cs.sanitize_proposal({"actions": bad + [{"action": "skip", "workout_id": a["id"]}]})
    assert [x["action"] for x in mixed["actions"]] == ["skip"]                           # one valid action among the junk survives


def test_another_users_workout_ids_cannot_be_proposed(env):
    other_id = env.db.create_user("b@example.com", "B", "x")
    udb2 = env.db.for_user(other_id)
    udb2.save_profile(env.udb.get_profile())
    from coach.service import CoachService
    svc2 = CoachService(udb2, Settings(db_path=env.db.path), env.coach, FakeStrava(), clock=lambda: TODAY)
    svc2.generate_plan()
    theirs = next(w for w in udb2.get_workouts(TODAY + timedelta(days=1)) if w["status"] == "planned" and w["type"] == "easy")
    assert service(env).sanitize_proposal({"actions": [{"action": "skip", "workout_id": theirs["id"]}]}) is None
    assert udb2.get_workout(theirs["id"])["status"] == "planned"


def test_at_most_three_actions_and_summary_is_trimmed(env):
    ws = [w for w in env.udb.get_workouts(TODAY + timedelta(days=1)) if w["status"] == "planned" and w["type"] == "easy"][:5]
    p = service(env).sanitize_proposal({"summary": "s" * 900, "actions": [{"action": "skip", "workout_id": w["id"]} for w in ws]})
    assert len(p["actions"]) == MAX_ACTIONS and len(p["summary"]) == 300


def test_pain_always_gets_an_ease_off_proposal_even_if_the_model_forgets(env):
    cs = service(env, FakeChat("Rest up and see someone if it persists."))
    out = cs.send("my left knee really hurts on stairs")
    assert out["proposal"]["actions"][0]["action"] == "ease_off" and out["proposal"]["status"] == "pending"
    a, _ = two_easy(env)
    cs2 = service(env, FakeChat("Skipping is wise.", {"summary": "Skip", "actions": [{"action": "skip", "workout_id": a["id"]}]}))
    out2 = cs2.send("I have a sore calf")
    assert [x["action"] for x in out2["proposal"]["actions"]] == ["ease_off", "skip"]
    cs3 = service(env, FakeChat("ok", {"summary": "Ease", "actions": [{"action": "ease_off"}]}))
    assert [x["action"] for x in cs3.send("shin pain again")["proposal"]["actions"]] == ["ease_off"]       # no duplicate
    assert service(env, FakeChat("Great!")).send("felt strong today")["proposal"] is None                  # no false alarm


# ------------------------------------------------------------------------------------------------- applying -----
def propose(env, actions, summary="Do it"):
    cs = service(env, FakeChat("Here's my suggestion.", {"summary": summary, "actions": actions}))
    return cs, cs.send("please change my plan")["proposal"]["id"]


def test_applying_a_proposal_performs_every_action_and_marks_it_applied(env):
    a, b = two_easy(env)
    target = free_usual_day(env, after_days=6)
    cs, mid = propose(env, [{"action": "skip", "workout_id": a["id"]}, {"action": "move", "workout_id": b["id"], "to_date": target.isoformat()}, {"action": "ease_off"}])
    assert env.udb.get_workout(a["id"])["status"] == "planned"                           # nothing happens until Apply
    res = cs.apply(mid)
    assert len(res["done"]) == 3 and res["replan"] is None
    assert env.udb.get_workout(a["id"])["status"] == "skipped" and env.udb.get_workout(b["id"])["date"] == target.isoformat()
    assert env.svc.pain_until() and env.udb.get_chat_message(mid)["proposal_status"] == "applied"
    with pytest.raises(UserError, match="already been handled"):
        cs.apply(mid)                                                                    # no double-apply


def test_actions_are_revalidated_at_apply_time(env):
    a, b = two_easy(env)
    cs, mid = propose(env, [{"action": "skip", "workout_id": a["id"]}, {"action": "skip", "workout_id": b["id"]}])
    env.udb.update_workout(a["id"], status="completed")                                  # the athlete ran it before tapping Apply
    res = cs.apply(mid)
    assert len(res["done"]) == 1 and len(res["problems"]) == 1 and "upcoming" in res["problems"][0]
    assert env.udb.get_workout(a["id"])["status"] == "completed" and env.udb.get_workout(b["id"])["status"] == "skipped"


def test_swap_and_replan_and_dismiss(env):
    a, b = two_easy(env)
    cs, mid = propose(env, [{"action": "swap", "workout_id": a["id"], "other_workout_id": b["id"]}])
    cs.apply(mid)
    assert env.udb.get_workout(a["id"])["date"] == b["date"] and env.udb.get_workout(b["id"])["date"] == a["date"]
    cs, mid = propose(env, [{"action": "replan", "note": "travelling Thu-Sat"}])
    assert cs.apply(mid)["replan"] == "travelling Thu-Sat"
    cs, mid = propose(env, [{"action": "ease_off"}])
    cs.dismiss(mid)
    assert env.udb.get_chat_message(mid)["proposal_status"] == "dismissed" and env.svc.pain_until() is None
    with pytest.raises(UserError):
        cs.apply(mid)
    with pytest.raises(UserError):
        cs.dismiss(mid)


def test_nobody_can_apply_someone_elses_suggestion_or_a_normal_message(env):
    cs, mid = propose(env, [{"action": "ease_off"}])
    other = env.db.create_user("b@example.com", "B", "x")
    from coach.service import CoachService
    cs2 = ChatService(CoachService(env.db.for_user(other), Settings(db_path=env.db.path), env.coach, FakeStrava(), clock=lambda: TODAY), FakeChat())
    with pytest.raises(UserError, match="wasn't found"):
        cs2.apply(mid)
    with pytest.raises(UserError, match="wasn't found"):
        cs2.dismiss(mid)
    plain = env.udb.recent_chat()[0]["id"]                                               # a user message, not a proposal
    with pytest.raises(UserError, match="wasn't found"):
        cs.apply(plain)
    assert env.svc.pain_until() is None


# ------------------------------------------------------------------------------------------------------ web -----
def web(tmp_path, chat):
    s = Settings(db_path=str(tmp_path / "w.db"), sync_interval_min=0, chat_daily_limit=3)
    db = DB(s.db_path)
    app = create_app(s, db=db, coach=ReferenceCoach(), strava_factory=lambda u: FakeStrava(), clock=lambda: TODAY, chat=chat)
    c = TestClient(app)
    signup(c)
    c.post("/setup", data={**SETUP, "baseline": "25"})
    c.post("/plan/generate")
    assert wait_job(c)["status"] == "done"
    return c, db


def post_chat(c, message):
    return c.post("/api/chat", json={"message": message}, headers={"X-Requested-With": "fetch"})


def test_page_has_the_tab_composer_and_starter_questions(tmp_path):
    c, _ = web(tmp_path, FakeChat())
    page = c.get("/coach").text
    assert 'href="/coach"' in page and ">Coach<" in page and 'name="message"' in page and "3 of 3 messages left today" in page.replace("<span id=\"remaining\">3</span>", "3")
    assert page.count("chip-btn") >= 4 and "Changes only happen when you tap Apply" in page


def test_api_chat_roundtrip_and_error_mapping(tmp_path):
    chat = FakeChat("Run easy tomorrow.")
    c, db = web(tmp_path, chat)
    r = post_chat(c, "what should I do tomorrow?")
    assert r.status_code == 200 and r.json()["reply"] == "Run easy tomorrow." and r.json()["remaining"] == 2 and r.json()["proposal"] is None
    assert "Run easy tomorrow." in c.get("/coach").text and "what should I do tomorrow?" in c.get("/coach").text
    assert post_chat(c, "   ").status_code == 400 and post_chat(c, "x" * 700).status_code == 400
    assert c.post("/api/chat", content="not json", headers={"Content-Type": "application/json"}).status_code == 400
    post_chat(c, "two")
    post_chat(c, "three")
    blocked = post_chat(c, "four")
    assert blocked.status_code == 429 and "chat messages" in blocked.json()["error"] and len(chat.calls) == 3
    chat.raises = None
    c2, _ = web(tmp_path / "x", FakeChat(raises=RuntimeError("kaboom")))
    err = post_chat(c2, "hi")
    assert err.status_code == 502 and "kaboom" in err.json()["error"]


def test_friendly_message_when_the_api_key_is_wrong(tmp_path):
    import anthropic
    import httpx2
    resp = httpx2.Response(401, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    c, _ = web(tmp_path, FakeChat(raises=anthropic.AuthenticationError("bad", response=resp, body=None)))
    r = post_chat(c, "hi")
    assert r.status_code == 502 and "rejected the API key" in r.json()["error"]


def test_api_chat_requires_login(tmp_path):
    c, _ = web(tmp_path, FakeChat())
    anon = TestClient(c.app)
    r = anon.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 401 and r.json()["status"] == "unauthenticated"


def test_proposal_cards_apply_and_dismiss_through_the_page(tmp_path):
    chat = FakeChat("Let's ease off.", {"summary": "Rest week", "actions": [{"action": "ease_off"}]})
    c, db = web(tmp_path, chat)
    j = post_chat(c, "I'm exhausted").json()
    page = c.get("/coach").text
    assert "Suggested change" in page and "Ease off for 7 days" in page and f"/coach/proposal/{j['proposal']['id']}/apply" in page
    assert db.for_user(1).kv_get("pain_until") is None
    r = c.post(f"/coach/proposal/{j['proposal']['id']}/apply", follow_redirects=True)
    assert "Done:" in r.text and db.for_user(1).kv_get("pain_until")
    assert "Applied" in c.get("/coach").text and f"/coach/proposal/{j['proposal']['id']}/apply" not in c.get("/coach").text
    j2 = post_chat(c, "again").json()
    c.post(f"/coach/proposal/{j2['proposal']['id']}/dismiss")
    assert "Dismissed" in c.get("/coach").text


def test_a_replan_proposal_starts_the_normal_review_job(tmp_path):
    chat = FakeChat("I'll re-plan.", {"summary": "Re-plan", "actions": [{"action": "replan", "note": "away this weekend"}]})
    c, db = web(tmp_path, chat)
    j = post_chat(c, "I'm away this weekend").json()
    c.post(f"/coach/proposal/{j['proposal']['id']}/apply")
    job = wait_job(c)
    assert job["kind"] == "review" and job["status"] == "done"
    assert any("away this weekend" in m["message"] for m in db.for_user(1).recent_log(5))


def test_plain_form_fallback_and_clear(tmp_path):
    c, db = web(tmp_path, FakeChat("Fine."))
    r = c.post("/coach/send", data={"message": "hello"}, follow_redirects=True)
    assert "Fine." in r.text and len(db.for_user(1).recent_chat()) == 2
    assert "Type a message" in c.post("/coach/send", data={"message": " "}, follow_redirects=True).text
    c.post("/coach/clear")
    assert db.for_user(1).recent_chat() == []


def test_chats_and_proposals_are_private_between_users(tmp_path):
    chat = FakeChat("Ease off.", {"summary": "x", "actions": [{"action": "ease_off"}]})
    c, db = web(tmp_path, chat)
    j = post_chat(c, "zebra marmalade").json()
    b = TestClient(c.app)
    signup(b, "b@example.com", "B")
    b.post("/setup", data={**SETUP, "baseline": "25"})
    assert "zebra marmalade" not in b.get("/coach").text and "Ease off." not in b.get("/coach").text
    r = b.post(f"/coach/proposal/{j['proposal']['id']}/apply", follow_redirects=True)
    assert "wasn&#39;t found" in r.text and db.for_user(1).kv_get("pain_until") is None and db.for_user(2).kv_get("pain_until") is None


def test_chat_is_deleted_with_the_account(tmp_path):
    c, db = web(tmp_path, FakeChat("hi"))
    post_chat(c, "hello")
    assert db.for_user(1).recent_chat()
    c.post("/account/delete", data={"confirm": "a@example.com"})
    assert db.for_user(1).recent_chat() == []
