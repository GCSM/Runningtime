"""Match Strava runs to planned workouts and compute weekly actuals."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from statistics import median

from .skeleton import monday


def match_workouts(workouts: list[dict], activities: list[dict], today: date) -> dict[int, dict]:
    """Return {workout_id: {status, actual_km, activity_id}}. One run per workout, same calendar day;
    the longest run of the day is the match. Past workouts with no run are 'missed'."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for a in activities:
        by_day[a["date"]].append(a)
    out: dict[int, dict] = {}
    for w in workouts:
        runs = sorted(by_day.get(w["date"], []), key=lambda a: -a["distance_m"])
        if not runs:
            status = "planned" if date.fromisoformat(w["date"]) >= today else "missed"
            out[w["id"]] = {"status": status, "actual_km": None, "activity_id": None}
            continue
        a = runs[0]
        km = a["distance_m"] / 1000
        if w.get("distance_km"):
            ratio = km / w["distance_km"]
        elif w.get("duration_min"):
            ratio = a["moving_s"] / 60 / w["duration_min"]
        else:
            ratio = 1.0
        out[w["id"]] = {"status": "completed" if ratio >= 0.8 else "partial", "actual_km": round(km, 2), "activity_id": a["id"]}
    return out


def weekly_km(activities: list[dict]) -> dict[date, float]:
    totals: dict[date, float] = defaultdict(float)
    for a in activities:
        totals[monday(date.fromisoformat(a["date"]))] += a["distance_m"] / 1000
    return dict(totals)


def recent_baseline_km(activities: list[dict], today: date, weeks: int = 4, fill_km: float = 0.0) -> float | None:
    """Mean weekly km over the last `weeks` *complete* weeks, or None if there are no complete weeks of history.

    Weeks before the athlete's first recorded run are *unknown*, not zero (a runner who connected Strava two weeks ago hasn't stopped
    running before that): they count as `fill_km` (the stated weekly distance). Zeros after the first run are real. A planned step-back
    week (or one off week) among otherwise steady weeks is ignored, so a plan rebuilt every Monday doesn't forget its own progression.
    """
    if not activities:
        return None
    totals = weekly_km(activities)
    this_wk = monday(today)
    first_wk = monday(min(date.fromisoformat(a["date"]) for a in activities))
    if first_wk >= this_wk:
        return None                                   # only this (incomplete) week so far
    vals = [totals.get(this_wk - timedelta(weeks=i), 0.0) if this_wk - timedelta(weeks=i) >= first_wk else fill_km for i in range(1, weeks + 1)]
    top = max(vals)
    if weeks >= 4 and top > 0 and sum(1 for v in vals if v >= 0.5 * top) >= weeks - 1:
        vals = sorted(vals, reverse=True)[:2]         # the two best weeks represent current capability; one step-back week doesn't count against it
    return sum(vals) / len(vals)


def easy_pace_s_km(activities: list[dict], today: date, days: int = 28) -> float | None:
    """Median pace of recent runs >= 3km; a rough proxy for easy pace."""
    since = (today - timedelta(days=days)).isoformat()
    paces = [a["moving_s"] / (a["distance_m"] / 1000) for a in activities if a["date"] >= since and a["distance_m"] >= 3000]
    return median(paces) if len(paces) >= 3 else None
