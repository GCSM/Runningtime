"""Milestones: earned from the athlete's real runs and plan, each with the date it was first earned.

Deterministic and idempotent: the same data always gives the same badges, so celebrating "new" ones only needs a list of
the ids already shown (stored per user)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from . import knowledge as K
from .skeleton import monday


@dataclass
class Ctx:
    acts: list[dict]            # all activities, any order
    workouts: list[dict]
    today: date
    goal_km: float
    goal_time_s: int | None
    plan_start: date | None
    race_date: date | None


def _sorted(acts):
    return sorted(acts, key=lambda a: a["start_local"])


def _nth_run(n: int) -> Callable[[Ctx], str | None]:
    return lambda c: (_sorted(c.acts)[n - 1]["date"] if len(c.acts) >= n else None)


def _first_run_over(km: float) -> Callable[[Ctx], str | None]:
    def f(c: Ctx):
        return next((a["date"] for a in _sorted(c.acts) if a["distance_m"] >= km * 1000 * 0.995), None)
    return f


def _total_km(target: float) -> Callable[[Ctx], str | None]:
    def f(c: Ctx):
        total = 0.0
        for a in _sorted(c.acts):
            total += a["distance_m"] / 1000
            if total >= target:
                return a["date"]
        return None
    return f


def _streak(n: int) -> Callable[[Ctx], str | None]:
    def f(c: Ctx):
        run = 0
        for w in sorted((w for w in c.workouts if date.fromisoformat(w["date"]) <= c.today), key=lambda w: w["date"]):
            if w["status"] == "missed":
                run = 0
            elif w["status"] in ("completed", "partial"):
                run += 1
                if run >= n:
                    return w["date"]
        return None
    return f


def _perfect_week(c: Ctx) -> str | None:
    weeks: dict[str, list[dict]] = {}
    for w in c.workouts:
        weeks.setdefault(w["week_start"], []).append(w)
    for ws in sorted(weeks):
        items = [w for w in weeks[ws] if w["type"] != "race"]
        if len(items) >= 3 and all(w["status"] == "completed" for w in items):
            return max(w["date"] for w in items)
    return None


def _first_hard_session(c: Ctx) -> str | None:
    return next((w["date"] for w in sorted(c.workouts, key=lambda w: w["date"])
                 if w["type"] in K.QUALITY_TYPES and w["status"] in ("completed", "partial")), None)


def _halfway(c: Ctx) -> str | None:
    if not c.plan_start or not c.race_date or (c.race_date - c.plan_start).days < 8 * 7:
        return None
    mid = c.plan_start + (c.race_date - c.plan_start) / 2
    return mid.isoformat() if c.today >= mid else None


def _race(c: Ctx) -> dict | None:
    race = next((w for w in c.workouts if w["type"] == "race" and w["status"] in ("completed", "partial") and w["activity_id"]), None)
    act = next((a for a in c.acts if race and a["id"] == race["activity_id"]), None)
    return {"workout": race, "act": act} if race and act else None


def _race_finisher(c: Ctx) -> str | None:
    r = _race(c)
    return r["workout"]["date"] if r and r["act"]["distance_m"] >= c.goal_km * 1000 * 0.97 else None


def _goal_time(c: Ctx) -> str | None:
    r = _race(c)
    if r and c.goal_time_s and r["act"]["distance_m"] >= c.goal_km * 1000 * 0.97 and r["act"]["moving_s"] <= c.goal_time_s:
        return r["workout"]["date"]
    return None


# id, name, emoji, how to earn it, evaluator
CATALOG: list[tuple[str, str, str, str, Callable[[Ctx], str | None]]] = [
    ("first_run", "First run logged", "👟", "Log your first run", _nth_run(1)),
    ("runs_10", "10 runs", "🔟", "Log 10 runs", _nth_run(10)),
    ("runs_25", "25 runs", "💪", "Log 25 runs", _nth_run(25)),
    ("runs_50", "50 runs", "🔥", "Log 50 runs", _nth_run(50)),
    ("runs_100", "100 runs", "💯", "Log 100 runs", _nth_run(100)),
    ("first_5k", "First 5K run", "🥉", "Run 5 km in one go", _first_run_over(5)),
    ("first_10k", "First 10K run", "🥈", "Run 10 km in one go", _first_run_over(10)),
    ("first_half", "Half-marathon distance", "🥇", "Run 21.1 km in one go", _first_run_over(21.0975)),
    ("first_marathon", "Marathon distance", "🏅", "Run 42.2 km in one go", _first_run_over(42.195)),
    ("km_100", "100 km total", "🛣️", "Run 100 km in total", _total_km(100)),
    ("km_250", "250 km total", "🌍", "Run 250 km in total", _total_km(250)),
    ("km_500", "500 km total", "🚀", "Run 500 km in total", _total_km(500)),
    ("km_1000", "1,000 km total", "🌟", "Run 1,000 km in total", _total_km(1000)),
    ("streak_5", "5 in a row", "⚡", "Complete 5 planned runs without a miss", _streak(5)),
    ("streak_10", "10 in a row", "🔋", "Complete 10 planned runs without a miss", _streak(10)),
    ("streak_20", "20 in a row", "🧱", "Complete 20 planned runs without a miss", _streak(20)),
    ("perfect_week", "Perfect week", "✅", "Complete every planned run in a week (3 or more)", _perfect_week),
    ("first_hard", "First hard session", "🎯", "Complete a tempo or interval session", _first_hard_session),
    ("halfway", "Halfway there", "⛰️", "Reach the midpoint of a plan of 8+ weeks", _halfway),
    ("race_finisher", "Race finisher", "🏁", "Finish your goal race", _race_finisher),
    ("goal_time", "Goal time!", "🏆", "Finish your race at or under your goal time", _goal_time),
]


def evaluate(ctx: Ctx) -> list[dict]:
    """Every badge with its earned date (None = locked), in catalogue order."""
    return [{"id": i, "name": n, "emoji": e, "hint": h, "earned": ev(ctx)} for i, n, e, h, ev in CATALOG]
