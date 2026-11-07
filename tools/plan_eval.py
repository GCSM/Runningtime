#!/usr/bin/env python
"""Plan evaluation: build the exact request the real coach sends for realistic athletes, get a plan from a model, and audit it.

  python tools/plan_eval.py capture OUT      write system/prompt/tool/profile files for each scenario (no network)
  python tools/plan_eval.py live OUT         call the REAL coach model (needs ANTHROPIC_API_KEY) and save model_output.json per scenario
  python tools/plan_eval.py audit OUT        replay each model_output.json through the app's real parsing + sanitizer, then audit
                                             the raw answer and the final plan (rules and shape) with coach/planaudit.py

Any model_output.json (a JSON object with "summary" and "workouts", exactly the submit_plan tool input) can be audited, so a plan from another
source (a person, another model) can be checked the same way. Run `live` + `audit` after any change to the prompt, the skeleton or the rules.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import tempfile
from datetime import date, timedelta
from types import SimpleNamespace as NS

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from coach import service as svc_mod  # noqa: E402
from coach.config import Settings  # noqa: E402
from coach.db import DB  # noqa: E402
from coach.llm import Coach  # noqa: E402
from coach.models import DAYS, DayAvail, Profile  # noqa: E402
from coach.planaudit import audit_plan, audit_shape, weeks_from_workouts  # noqa: E402
from coach.service import CoachService  # noqa: E402
from coach.skeleton import monday  # noqa: E402

TODAY = date(2026, 11, 9)       # a Monday: scenarios are dated so results don't depend on when they are run


class _Strava:
    connected = False
    configured = False


def avail(days, mins, long_day, long_min):
    return {d: DayAvail(available=d in days, minutes=long_min if d == long_day else mins.get(d, 60)) for d in DAYS}


SCENARIOS = {
    "5k_beginner": dict(label="5K", km=5.0, weeks=10, level="beginner", base=8.0, goal_time=None, av=avail(("tue", "thu", "sun"), {"tue": 40, "thu": 40}, "sun", 60)),
    "10k_intermediate": dict(label="10K", km=10.0, weeks=12, level="intermediate", base=25.0, goal_time=50 * 60,
                             av=avail(("tue", "wed", "fri", "sun"), {"tue": 60, "wed": 60, "fri": 60}, "sun", 90)),
    "half_intermediate": dict(label="Half marathon", km=21.0975, weeks=14, level="intermediate", base=30.0, goal_time=110 * 60,
                              av=avail(("mon", "tue", "thu", "sat", "sun"), {"mon": 60, "tue": 75, "thu": 75, "sat": 60}, "sun", 150)),
    "marathon_5day_24wk": dict(label="Marathon", km=42.195, weeks=24, level="intermediate", base=40.0, goal_time=4 * 3600,
                               av=avail(("mon", "tue", "thu", "sat", "sun"), {"mon": 60, "tue": 75, "thu": 75, "sat": 60}, "sun", 240)),
    "marathon_4day_20wk": dict(label="Marathon", km=42.195, weeks=20, level="intermediate", base=35.0, goal_time=None,
                               av=avail(("tue", "thu", "sat", "sun"), {"tue": 75, "thu": 75, "sat": 75}, "sun", 240)),
}


def profile_for(sc: dict) -> Profile:
    race = TODAY + timedelta(weeks=sc["weeks"], days=-1)
    race += timedelta(days=6 - race.weekday())             # the race is on a Sunday
    return Profile(name="Test Athlete", goal_label=sc["label"], goal_km=sc["km"], race_date=race, baseline_km=sc["base"], experience=sc["level"],
                   availability=sc["av"], long_run_day="sun", goal_time_s=sc["goal_time"], notes="", units="km")


def make_service(profile: Profile, coach) -> tuple[CoachService, object]:
    s = Settings(db_path=str(pathlib.Path(tempfile.mkdtemp()) / "c.db"), sync_interval_min=0)
    db = DB(s.db_path)
    udb = db.for_user(db.create_user("a@example.com", "Test Athlete", "x"))
    udb.save_profile(profile)
    return CoachService(udb, s, coach, _Strava(), clock=lambda: TODAY), udb


class _Stop(Exception):
    pass


class _Capture:
    def __init__(self, path):
        self.path, self.messages = path, self

    def create(self, **kw):
        (self.path / "system.txt").write_text(kw["system"])
        (self.path / "prompt.txt").write_text(kw["messages"][0]["content"])
        (self.path / "tool.json").write_text(json.dumps(kw["tools"][0], indent=1))
        raise _Stop()


def capture(out: pathlib.Path) -> None:
    for name, sc in SCENARIOS.items():
        d = out / name
        d.mkdir(parents=True, exist_ok=True)
        p = profile_for(sc)
        (d / "profile.json").write_text(p.model_dump_json(indent=1))
        svc, _ = make_service(p, Coach("model", client=_Capture(d)))
        try:
            svc.generate_plan()
        except _Stop:
            pass
        print(f"{name}: race {p.race_date}, prompt {len((d / 'prompt.txt').read_text())} chars")


def live(out: pathlib.Path, model: str = "claude-sonnet-5-5") -> None:
    for name, sc in SCENARIOS.items():
        d = out / name
        d.mkdir(parents=True, exist_ok=True)
        p = profile_for(sc)
        (d / "profile.json").write_text(p.model_dump_json(indent=1))
        seen = {}

        class Recorder(Coach):
            def plan(self, context, *, mode, feedback=None):
                r = super().plan(context, mode=mode, feedback=feedback)
                seen["out"] = r
                return r

        svc, _ = make_service(p, Recorder(model))
        svc.generate_plan()
        (d / "model_output.json").write_text(json.dumps(seen["out"], indent=1))
        print(f"{name}: {len(seen['out']['workouts'])} workouts")


class _SDK:
    def __init__(self, payload):
        self.payload, self.calls, self.messages = payload, 0, self

    def create(self, **kw):
        self.calls += 1
        return NS(stop_reason="tool_use", content=[NS(type="tool_use", name="submit_plan", input=self.payload)])


def audit(out: pathlib.Path) -> int:
    failures = 0
    for d in sorted(p for p in out.iterdir() if p.is_dir() and (p / "model_output.json").exists()):
        model_out = json.loads((d / "model_output.json").read_text())
        p = Profile.model_validate_json((d / "profile.json").read_text())
        notes_log: list[list[str]] = []
        real = svc_mod.sanitize

        def spy(*a, **k):
            r = real(*a, **k)
            notes_log.append(r[1])
            return r

        svc_mod.sanitize = spy
        sdk = _SDK(model_out)
        svc, udb = make_service(p, Coach("model", client=sdk))
        try:
            svc.generate_plan()
            error = None
        except Exception as e:  # noqa: BLE001
            error = str(e)
        finally:
            svc_mod.sanitize = real
        print(f"\n=== {d.name}: {len(model_out['workouts'])} workouts from the model, {sdk.calls} call(s)")
        if error:
            print("  REJECTED by the app:", error)
            failures += 1
            continue
        skel = svc._skeleton(p, [], TODAY).weeks
        final = [dict(w) for w in udb.get_workouts()]
        raw_rules = audit_plan([{**w, "week_start": monday(date.fromisoformat(w["date"])).isoformat()} for w in model_out["workouts"]], p, skel, TODAY)
        final_rules = audit_plan(final, p, skel, TODAY)
        shape = audit_shape(weeks_from_workouts(final, skel), p.goal_km, p.experience)
        changed = sum(len(n) for n in notes_log)
        words = len(model_out["summary"].split())
        print(f"  app corrected {changed} thing(s); summary {words} words")
        for label, probs in (("model's raw plan, rule problems", raw_rules), ("final plan, rule problems", final_rules), ("final plan, SHAPE problems", shape)):
            print(f"  {label}: {len(probs)}")
            for x in probs[:8]:
                print("     -", x)
        failures += bool(final_rules or shape)
        weeks_ = weeks_from_workouts(final, skel)
        print("  weekly km:", " ".join(f"{w.target_km:.0f}" for w in weeks_))
        print("  long run :", " ".join(f"{w.long_run_km:.0f}" for w in weeks_))
    return failures


def compare(out: pathlib.Path) -> None:
    """Same scenarios, same audit: the code-built planner against the plan the model wrote (model_output.json)."""
    from coach.builder import BuilderCoach
    from coach.plancheck import is_paced_long
    from coach import knowledge as K

    def stats(ws, p, skel):
        hard = [w for w in ws if w["type"] in K.QUALITY_TYPES or is_paced_long(w)]
        kinds = sorted({re.sub(r"[\d.:×x]+|\s+", " ", w["title"]).strip().lower() for w in ws if w["type"] in K.QUALITY_TYPES})
        weeks_ = weeks_from_workouts(ws, skel)
        train = [w for w in weeks_ if w.phase in ("base", "build", "peak")]
        return dict(workouts=len(ws), hard=len(hard), distinct_sessions=len(kinds), peak_km=max((w.target_km for w in train), default=0),
                    peak_long=max((w.long_run_km for w in train), default=0), rules=len(audit_plan(ws, p, skel, TODAY)),
                    shape=len(audit_shape(weeks_, p.goal_km, p.experience)), avg_week_pct=0)

    for d in sorted(x for x in out.iterdir() if x.is_dir() and (x / "profile.json").exists()):
        p = Profile.model_validate_json((d / "profile.json").read_text())
        rows = {}
        for who, coach in (("builder", BuilderCoach()),) + ((("model", Coach("model", client=_SDK(json.loads((d / "model_output.json").read_text())))),)
                                                           if (d / "model_output.json").exists() else ()):
            notes: list[list[str]] = []
            real = svc_mod.sanitize
            svc_mod.sanitize = lambda *a, _r=real, **k: (lambda r: (notes.append(r[1]), r)[1])(_r(*a, **k))
            svc, udb = make_service(p, coach)
            try:
                svc.generate_plan()
                skel = svc._skeleton(p, [], TODAY).weeks
                rows[who] = {**stats([dict(w) for w in udb.get_workouts()], p, skel), "guard_fixes": sum(len(n) for n in notes)}
            except Exception as e:  # noqa: BLE001
                rows[who] = {"error": str(e)[:80]}
            finally:
                svc_mod.sanitize = real
        print(f"\n=== {d.name}")
        for who, r in rows.items():
            print(f"  {who:8}", r)


if __name__ == "__main__":
    cmd, out = (sys.argv[1], pathlib.Path(sys.argv[2])) if len(sys.argv) >= 3 else (None, None)
    if cmd not in ("capture", "live", "audit", "compare"):
        print(__doc__)
        sys.exit(2)
    out.mkdir(parents=True, exist_ok=True)
    if cmd == "capture":
        capture(out)
    elif cmd == "live":
        live(out)
    elif cmd == "compare":
        compare(out)
    else:
        sys.exit(1 if audit(out) else 0)
