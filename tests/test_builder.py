"""The code-built planner (coach/builder.py): rules checked exactly, then across hundreds of athletes through the real service.

The planner replaces the language model for the day-by-day plan, so it carries the same burden of proof: every rule the guard and the audit
know must hold for what it writes WITHOUT the guard having to repair it (a good builder needs no fixing)."""
import itertools
import pathlib
import re
import tempfile
from datetime import date, timedelta

import pytest

from coach import knowledge as K
from coach.builder import (HMP_BLOCK_KM, MP_BLOCK_KM, RUN_WALK, BuilderCoach, build, run_walk_for, summary_text, window_ok)
from coach.config import Settings
from coach.db import DB
from coach.models import DAYS, DayAvail, Profile
from coach.planaudit import audit_plan
from coach.plancheck import is_paced_long
from coach.service import CoachService
from coach.skeleton import Week, monday

TODAY = date(2026, 11, 9)      # a Monday
SCHED = {3: ("tue", "thu", "sun"), 4: ("tue", "thu", "sat", "sun"), 5: ("mon", "tue", "thu", "sat", "sun"), 6: ("mon", "tue", "wed", "thu", "sat", "sun")}
GOAL_TIME = {5.0: 28 * 60, 10.0: 3600, 21.0975: 7200, 42.195: 15300}


class Offline:
    connected = False


class Rec(BuilderCoach):
    def plan(self, ctx, *, mode, feedback=None):
        self.ctx = ctx
        self.last = super().plan(ctx, mode=mode, feedback=feedback)
        return self.last


def make(goal, level="intermediate", nd=4, base=25.0, weeks=16, units="km", mins=90, long_min=180, goal_time=True, today=TODAY, race_extra=6):
    p = Profile(name="x", goal_label=K.goal_name(goal), goal_km=goal, race_date=today + timedelta(weeks=weeks, days=race_extra), baseline_km=base, experience=level,
                availability={d: DayAvail(available=d in SCHED[nd], minutes=long_min if d == "sun" else mins) for d in DAYS}, long_run_day="sun",
                goal_time_s=GOAL_TIME[goal] if goal_time else None, units=units)
    s = Settings(db_path=str(pathlib.Path(tempfile.mkdtemp()) / "c.db"), sync_interval_min=0)
    db = DB(s.db_path)
    udb = db.for_user(db.create_user("a@example.com", "A", "x"))
    udb.save_profile(p)
    coach = Rec()
    svc = CoachService(udb, s, coach, Offline(), clock=lambda: today)
    return p, svc, udb, coach


def plan(goal, **kw):
    p, svc, udb, coach = make(goal, **kw)
    svc.generate_plan()
    return p, svc, udb, coach, udb.get_workouts()


def skeleton(udb):
    return [Week(date.fromisoformat(w["week_start"]), w["phase"], bool(w["recovery"]), bool(w["race_week"]), w["target_km"], w["long_run_km"]) for w in udb.get_weeks()]


def by_week(ws):
    return {k: list(v) for k, v in itertools.groupby(ws, key=lambda w: w["week_start"])}


# ---------------------------------------------------------------- across many athletes
def configs():
    for goal, level, nd, base in itertools.product((5.0, 10.0, 21.0975, 42.195), K.LEVELS, (3, 4, 5, 6), (0, 20, 45)):
        yield goal, level, nd, base, K.min_weeks(goal, level) + (4 if base == 20 else 0)


@pytest.mark.parametrize("goal", [5.0, 10.0, 21.0975, 42.195])
def test_every_hard_rule_holds_for_what_the_builder_writes_and_the_guard_has_nothing_to_repair(goal):
    n = changed = total = 0
    bad = []
    for g, level, nd, base, weeks in configs():
        if g != goal or weeks < 6:
            continue
        p, svc, udb, coach, ws = plan(g, level=level, nd=nd, base=base, weeks=weeks, mins=75 if nd >= 5 else 90)
        n += 1
        raw = {w["date"]: w for w in coach.last["workouts"]}
        total += len(raw)
        for w in ws:
            r = raw[w["date"]]
            changed += abs(r["distance_km"] - w["distance_km"]) > 0.15 or r["type"] != w["type"]
        who = f"{K.goal_name(g)} {level} {nd}d base{base} {weeks}wk"
        bad += [f"{who}: {v}" for v in audit_plan(ws, p, skeleton(udb), TODAY, check_volume=False)]
        assert len(ws) == len(raw), who                                   # nothing dropped by the guard
    assert n >= 25
    assert changed <= 0.002 * total, f"the guard repaired {changed} of {total} workouts"
    assert not bad, f"{len(bad)} rule violations, e.g.\n" + "\n".join(bad[:10])


@pytest.mark.parametrize("goal", [5.0, 10.0, 21.0975, 42.195])
def test_weeks_deliver_their_target_distance_except_where_the_athletes_days_cannot_carry_it(goal):
    short = []
    n = 0
    for g, level, nd, base, weeks in configs():
        if g != goal or weeks < 6 or nd < 4:
            continue                                   # three-day weeks are limited by day caps: covered by the skeleton's own warnings
        p, svc, udb, coach, ws = plan(g, level=level, nd=nd, base=base, weeks=weeks, mins=75 if nd >= 5 else 90)
        sk = {w["week_start"]: w for w in udb.get_weeks()}
        for wk, items in by_week(ws).items():
            s = sk[wk]
            if s["race_week"] or wk <= (TODAY + timedelta(days=6)).isoformat():
                continue
            n += 1
            if sum(w["distance_km"] for w in items) < 0.85 * s["target_km"]:
                short.append(f"{K.goal_name(g)} {level} {nd}d base{base} {wk}: {sum(w['distance_km'] for w in items):.1f} vs {s['target_km']}")
    assert n > 100
    assert len(short) <= 0.02 * n, f"{len(short)}/{n} weeks under 85% of target, e.g. {short[:5]}"


# ---------------------------------------------------------------- the content of a week
def test_a_marathon_week_looks_like_a_published_plan():
    p, svc, udb, coach, ws = plan(42.195, level="intermediate", nd=5, base=40, weeks=20, mins=80)
    weeks = by_week(ws)
    sk = {w["week_start"]: w for w in udb.get_weeks()}
    peak_wk = max((wk for wk in weeks if sk[wk]["phase"] == "peak" and not sk[wk]["recovery"]), key=lambda wk: sk[wk]["target_km"])
    days = {w["date"]: w for w in weeks[peak_wk]}
    long_run = next(w for w in weeks[peak_wk] if w["type"] == "long")
    assert DAYS[date.fromisoformat(long_run["date"]).weekday()] == "sun"
    hard = [w for w in weeks[peak_wk] if w["type"] in K.QUALITY_TYPES]
    assert 1 <= len(hard) <= 2
    assert sum(w["type"] == "easy" for w in weeks[peak_wk]) >= 2           # most of the week is easy running
    assert all(abs((date.fromisoformat(h["date"]) - date.fromisoformat(long_run["date"])).days) >= 2 for h in hard)
    assert all("Warm up" in h["description"] and "Cool down" in h["description"] for h in hard)
    assert len(days) == len(weeks[peak_wk])


def test_sessions_progress_through_the_build_and_never_repeat_four_weeks_running():
    p, svc, udb, coach, ws = plan(10.0, level="intermediate", nd=5, base=30, weeks=20, mins=80)
    titles = [w["title"] for w in ws if w["type"] in K.QUALITY_TYPES]
    assert len(set(titles)) >= 6                                            # variety, not one session for 20 weeks
    chain = 0
    prev = None
    for wk, items in by_week(ws).items():
        q = [w["title"] for w in items if w["type"] in K.QUALITY_TYPES]
        chain = chain + 1 if q and q[0] == prev else 1
        prev = q[0] if q else None
        assert chain < 4
    mins = [int(m.group(1)) for t in titles if (m := re.match(r"Tempo (\d+) min", t))]
    assert mins and max(mins[len(mins) // 2:]) >= max(mins[: len(mins) // 2]) and max(mins) > min(mins)     # tempo runs get longer


def test_marathon_paces_and_paced_long_runs_only_appear_where_they_belong():
    for goal in (5.0, 10.0):
        _, _, _, _, ws = plan(goal, level="advanced", nd=5, base=40, weeks=16, mins=80)
        assert not any(is_paced_long(w) for w in ws)                        # no marathon-pace long runs in a 5K or 10K plan
    p, svc, udb, coach, ws = plan(42.195, level="intermediate", nd=5, base=40, weeks=24, mins=80)
    sk = {w["week_start"]: w for w in udb.get_weeks()}
    paced = [w for w in ws if is_paced_long(w)]
    assert paced and all(sk[w["week_start"]]["phase"] in ("build", "peak") and not sk[w["week_start"]]["recovery"] for w in paced)
    blocks = [int(re.search(r"(\d+) km at marathon pace", w["title"]).group(1)) for w in paced]
    assert blocks == sorted(blocks) and max(blocks) >= 10 and len(blocks) >= 4                  # marathon-pace blocks grow to 10+ km across several long runs
    for w in paced:
        block = int(re.search(r"(\d+) km at marathon pace", w["title"]).group(1))
        assert block <= 0.5 * w["distance_km"] + 0.6 and block <= max(MP_BLOCK_KM)
        same_week = [x for x in ws if x["week_start"] == w["week_start"] and x["type"] in K.QUALITY_TYPES]
        assert len(same_week) <= 1                                          # a paced long run counts as a hard effort
    half = plan(21.0975, level="intermediate", nd=5, base=30, weeks=18, mins=80)[4]
    assert any(is_paced_long(w) for w in half) and all(int(re.search(r"(\d+) km at half", w["title"]).group(1)) in HMP_BLOCK_KM for w in half if is_paced_long(w))


def test_easy_long_runs_are_not_mistaken_for_hard_ones_by_the_guard():
    _, _, _, _, ws = plan(42.195, level="beginner", nd=4, base=20, weeks=30)
    for w in ws:
        if w["type"] == "long" and "marathon pace" not in w["title"]:
            assert not is_paced_long(w), w["description"]


def test_race_week_has_the_race_a_shakeout_and_nothing_hard_in_the_last_two_days():
    for goal in (5.0, 10.0, 21.0975, 42.195):
        p, svc, udb, coach, ws = plan(goal, level="intermediate", nd=5, base=35, weeks=14, mins=80)
        race = next(w for w in ws if w["type"] == "race")
        assert race["date"] == p.race_date.isoformat() and abs(race["distance_km"] - p.goal_km) < 0.1
        last_days = [w for w in ws if 0 < (p.race_date - date.fromisoformat(w["date"])).days <= 2]
        assert all(w["type"] == "easy" for w in last_days)
        assert any("Shakeout" in w["title"] for w in ws if w["week_start"] == race["week_start"])
        touch = [w for w in ws if w["week_start"] == race["week_start"] and w["type"] in K.QUALITY_TYPES]
        assert len(touch) <= 1 and all((p.race_date - date.fromisoformat(w["date"])).days >= 3 for w in touch)


def test_a_race_early_in_the_week_is_planned_around():
    for extra in (0, 1, 2):
        p, svc, udb, coach, ws = plan(10.0, level="intermediate", nd=5, base=30, weeks=10, mins=80, race_extra=extra)
        assert sum(w["type"] == "race" for w in ws) == 1
        assert all(date.fromisoformat(w["date"]) <= p.race_date for w in ws)
        assert not [v for v in audit_plan(ws, p, skeleton(udb), TODAY, check_volume=False)]


def test_hard_efforts_never_stack_more_than_two_in_seven_days():
    assert window_ok(date(2026, 11, 10), [date(2026, 11, 8)])
    assert not window_ok(date(2026, 11, 12), [date(2026, 11, 8), date(2026, 11, 10)])
    assert window_ok(date(2026, 11, 16), [date(2026, 11, 8), date(2026, 11, 10)])


# ---------------------------------------------------------------- beginners
def test_a_beginner_starts_with_run_walk_that_progresses_a_rung_a_week_and_has_no_hard_session_meanwhile():
    p, svc, udb, coach, ws = plan(5.0, level="beginner", nd=3, base=0, weeks=14)
    rw = [w for w in ws if w["title"].startswith("Run/walk")]
    assert rw and rw[0]["date"] <= (TODAY + timedelta(days=7)).isoformat()
    first = int(re.search(r"\((\d+(?::\d+)?) min run", rw[0]["title"]).group(1).split(":")[0]) if "min run" in rw[0]["title"] else 0
    assert first <= 2                                                       # begins with short running segments
    rw_weeks = sorted({w["week_start"] for w in rw})
    for wk in rw_weeks:
        assert not [w for w in by_week(ws)[wk] if w["type"] in K.QUALITY_TYPES]       # no hard session in a run/walk week
    # the running segment never jumps by more than a rung, and every session holds at least three cycles
    for w in rw:
        run_s = re.search(r"\((\d+) min run|\((\d+):(\d\d) run", w["title"])
        cycles = int(re.match(r"Run/walk (\d+) ×", w["title"]).group(1))
        assert cycles >= 3
    assert run_walk_for(RUN_WALK[-1], 20) in RUN_WALK and run_walk_for(RUN_WALK[-1], 20)[0] < 900


def test_an_experienced_runner_never_gets_run_walk():
    for level in ("intermediate", "advanced"):
        _, _, _, _, ws = plan(10.0, level=level, nd=4, base=0, weeks=12)
        assert not any(w["title"].startswith("Run/walk") for w in ws)


# ---------------------------------------------------------------- units, pain, text
def test_miles_athletes_read_miles_and_never_kilometres_in_session_text():
    _, _, _, _, ws = plan(21.0975, level="intermediate", nd=5, base=30, weeks=14, units="mi", mins=80)
    for w in ws:
        text = f"{w['title']} {w['description']} {w['pace_guidance']}"
        assert not re.search(r"\bkm\b|/km", text), text
    assert any("/mi" in w["description"] for w in ws)


def test_kilometre_athletes_never_see_miles():
    _, _, _, _, ws = plan(10.0, level="intermediate", nd=4, base=30, weeks=10)
    assert not any(re.search(r"/mi\b|\bmiles?\b", f"{w['title']} {w['description']}") for w in ws)


def test_pain_in_the_check_in_removes_every_hard_session_and_paced_long_run():
    p, svc, udb, coach = make(21.0975, level="intermediate", nd=5, base=30, weeks=16)
    svc.generate_plan()
    assert any(w["type"] in K.QUALITY_TYPES for w in coach.last["workouts"])
    ctx = dict(coach.ctx)
    ctx["injury_or_pain_mentioned_in_check_in"] = True
    out = build(ctx)["workouts"]
    assert not [w for w in out if w["type"] in K.QUALITY_TYPES]
    assert not any(is_paced_long(w) for w in out)
    assert sum(w["type"] == "race" for w in out) == 1


def test_no_goal_time_still_gives_a_full_plan_prescribed_by_effort():
    p, svc, udb, coach, ws = plan(10.0, level="intermediate", nd=4, base=30, weeks=10, goal_time=False)
    assert any(w["type"] in K.QUALITY_TYPES for w in ws)
    assert all(w["pace_guidance"] for w in ws)


def test_summary_is_plain_and_mentions_the_goal_and_date():
    p, svc, udb, coach, ws = plan(10.0, level="intermediate", nd=4, base=30, weeks=10)
    s = summary_text(coach.ctx)
    assert "10K" in s and p.race_date.isoformat() in s and len(s.split()) < 120


# ---------------------------------------------------------------- the service around it
def test_weekly_review_rebuilds_from_what_was_actually_run_with_the_builder():
    p, svc, udb, coach, ws = plan(10.0, level="intermediate", nd=4, base=30, weeks=12)
    later = TODAY + timedelta(weeks=3, days=2)
    svc2 = CoachService(udb, Settings(db_path=str(pathlib.Path(tempfile.mkdtemp()) / "x.db"), sync_interval_min=0), coach, Offline(), clock=lambda: later)
    svc2.db = udb
    text = svc2.review(sync=False)
    new = [w for w in udb.get_workouts(later) if w["status"] == "planned"]
    assert new and all(w["date"] >= later.isoformat() for w in new) and text


def test_the_engine_switch_picks_the_builder_or_the_model(monkeypatch):
    from coach.llm import Coach
    from coach.web import make_planner
    assert isinstance(make_planner(Settings(plan_engine="builder")), BuilderCoach)
    assert isinstance(make_planner(Settings(plan_engine="model")), Coach)
    monkeypatch.setenv("PLAN_ENGINE", "Builder ")
    assert Settings.from_env().plan_engine == "builder"
    monkeypatch.delenv("PLAN_ENGINE")
    assert Settings.from_env().plan_engine == "model"                      # the model stays the default until the builder has proved itself


# ---------------------------------------------------------------- variety and benchmarks (gaps found against published planners)
@pytest.mark.parametrize("goal,kw", [(10.0, dict(level="intermediate", nd=4, base=30, weeks=14)), (21.0975, dict(level="intermediate", nd=4, base=25, weeks=18)),
                                     (42.195, dict(level="intermediate", nd=5, base=40, weeks=24, mins=80)), (42.195, dict(level="intermediate", nd=4, base=35, weeks=20))])
def test_the_second_half_of_a_plan_does_not_go_stale(goal, kw):
    """Garmin Coach's best-known complaint: after ~8 weeks the plan repeats itself. Sessions must keep changing while the plan builds."""
    p, svc, udb, coach, ws = plan(goal, **kw)
    sk = udb.get_weeks()
    half = sk[len(sk) // 2]["week_start"]
    q = [w for w in ws if w["type"] in K.QUALITY_TYPES and w["week_start"] >= half and "Goal-pace" not in w["title"]]
    assert len(q) >= 5 and len({w["title"] for w in q}) >= 0.8 * len(q)


def test_benchmark_time_trials_recalibrate_paces_and_sit_in_sensible_weeks():
    p, svc, udb, coach, ws = plan(10.0, level="intermediate", nd=4, base=30, weeks=14)
    sk = {w["week_start"]: w for w in udb.get_weeks()}
    bench = [w for w in ws if w["title"].startswith("Benchmark")]
    assert 1 <= len(bench) <= 2
    for w in bench:
        assert not sk[w["week_start"]]["recovery"] and w["type"] in K.QUALITY_TYPES
        assert "recalibrated" in w["description"] and "own activity" in w["description"]
        assert re.search(r"[35] km time trial", w["title"])
    assert not [w for w in plan(5.0, level="beginner", nd=3, base=0, weeks=12)[4] if w["title"].startswith("Benchmark")]       # beginners: no all-out efforts yet
    assert not [w for w in plan(10.0, level="intermediate", nd=4, base=30, weeks=5)[4] if w["title"].startswith("Benchmark")]  # nor in a very short plan


def test_a_completed_benchmark_moves_the_next_plans_paces():
    """The loop: benchmark run recorded -> fitness estimate -> training paces. A fast time trial makes the next plan's paces faster."""
    from coach import fitness as F
    slow = {"id": 1, "date": "2026-12-01", "distance_m": 3000, "moving_s": 16 * 60}
    fast = {"id": 2, "date": "2026-12-01", "distance_m": 3000, "moving_s": 13 * 60}
    assert F.run_vdot(fast) > F.run_vdot(slow)
    goal = 46.0
    assert K.working_vdot(goal, F.run_vdot(fast), 4) > K.working_vdot(goal, F.run_vdot(slow), 4)
    assert K.training_paces(K.working_vdot(goal, F.run_vdot(fast), 4))["threshold"][0] < K.training_paces(K.working_vdot(goal, F.run_vdot(slow), 4))["threshold"][0]
