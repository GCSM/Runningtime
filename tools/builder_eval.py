"""Run the code-built planner (no model) over many athletes through the real service and audit every plan.

Reports: audit violations (rules a plan must keep) and how much the sanitizer had to change (a good builder needs no fixing).
    python tools/builder_eval.py [--goal 42.195] [--show N]
"""
from __future__ import annotations

import argparse
import itertools
import re
import pathlib
import sys
import tempfile
from collections import Counter
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from coach import knowledge as K  # noqa: E402
from coach import plancheck  # noqa: E402
from coach.builder import BuilderCoach  # noqa: E402
from coach.config import Settings  # noqa: E402
from coach.db import DB  # noqa: E402
from coach.models import DAYS, DayAvail, Profile  # noqa: E402
from coach.planaudit import audit_plan, audit_shape, weeks_from_workouts  # noqa: E402
from coach.service import CoachService  # noqa: E402
from coach.skeleton import monday  # noqa: E402

TODAY = date(2026, 11, 9)


class Recording(BuilderCoach):
    def plan(self, ctx, *, mode, feedback=None):
        self.last = super().plan(ctx, mode=mode, feedback=feedback)
        return self.last


class Offline:
    connected = False

    def __getattr__(self, n):
        raise RuntimeError(n)


def make(goal, level, days, mins, long_min, weeks, base, goal_time=None, long_day="sun"):
    p = Profile(name="x", goal_label=K.goal_name(goal), goal_km=goal, race_date=TODAY + timedelta(weeks=weeks, days=6), baseline_km=base, experience=level,
                availability={d: DayAvail(available=d in days, minutes=long_min if d == long_day else mins) for d in DAYS}, long_run_day=long_day,
                goal_time_s=goal_time)
    s = Settings(db_path=str(pathlib.Path(tempfile.mkdtemp()) / "c.db"), sync_interval_min=0)
    db = DB(s.db_path)
    udb = db.for_user(db.create_user("a@example.com", "A", "x"))
    udb.save_profile(p)
    coach = Recording()
    svc = CoachService(udb, s, coach, Offline(), clock=lambda: TODAY)
    return p, svc, udb


SCHEDULES = {3: ("tue", "thu", "sun"), 4: ("tue", "thu", "sat", "sun"), 5: ("mon", "tue", "thu", "sat", "sun"), 6: ("mon", "tue", "wed", "thu", "sat", "sun")}
GOAL_TIMES = {5.0: 28 * 60, 10.0: 60 * 60, 21.0975: 2 * 3600, 42.195: 4 * 3600 + 15 * 60}


def configs(goals):
    for goal, level, nd, base, extra in itertools.product(goals, K.LEVELS, (3, 4, 5, 6), (0, 15, 35), (0, 6, 14)):
        weeks = K.min_weeks(goal, level) + extra
        if weeks >= 6:
            yield goal, level, nd, base, weeks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--goal", type=float, action="append")
    ap.add_argument("--show", type=int, default=12)
    ap.add_argument("--notime", action="store_true")
    a = ap.parse_args()
    goals = a.goal or [5.0, 10.0, 21.0975, 42.195]
    bad, fixed, n, errors = [], Counter(), 0, []
    for goal, level, nd, base, weeks in configs(goals):
        mins = 75 if nd >= 5 else 90
        who = f"{K.goal_name(goal)} {level} {nd}d base{base} {weeks}wk"
        p, svc, udb = make(goal, level, SCHEDULES[nd], mins, 180, weeks, base, None if a.notime else GOAL_TIMES[goal])
        try:
            svc.generate_plan()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{who}: {type(e).__name__}: {e}")
            continue
        n += 1
        raw = {w["date"]: w for w in svc.coach.last["workouts"]}
        saved = {w["date"]: w for w in udb.get_workouts()}
        for dt, w in raw.items():
            r = saved.get(dt)
            if r is None:
                fixed["dropped"] += 1
            elif abs(r["distance_km"] - w["distance_km"]) > 0.15:
                fixed["distance changed"] += 1
            elif r["type"] != w["type"]:
                fixed["type changed"] += 1
        fixed["workouts"] += len(raw)
        ws = udb.get_workouts()
        log = [r for r in udb.get_log() if r.get("kind") == "plan"] if hasattr(udb, "get_log") else []
        weeks_rows = [type("W", (), w) for w in []]
        from coach.skeleton import Week
        sk = [Week(date.fromisoformat(w["week_start"]), w["phase"], bool(w["recovery"]), bool(w["race_week"]), w["target_km"], w["long_run_km"]) for w in udb.get_weeks()]
        for v in audit_plan(ws, p, sk, TODAY):
            bad.append(f"{who}: {v}")
        for v in audit_shape(weeks_from_workouts(ws, sk), goal, level):
            bad.append(f"{who}: shape: {v}")
    print(f"{n} plans, {len(errors)} errors, {len(bad)} audit problems; sanitizer changes: {dict(fixed)}")
    kinds = Counter(re.sub(r"[\d.:-]+", "#", b.split(": ", 1)[1])[:70] for b in bad)
    for k, c in kinds.most_common(25):
        print(f"{c:5d}  {k}")
    for b in bad[: a.show]:
        print("  ", b)
    for e in errors[:10]:
        print("ERR", e)


if __name__ == "__main__":
    main()
