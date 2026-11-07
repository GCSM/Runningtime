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
        a = runs[0]                                           # the longest run is "the" run (its id is what the workout links to)…
        km = sum(r["distance_m"] for r in runs) / 1000        # …but a run split in two (or a warm-up jog plus the session) still counts in full
        moving = sum(r["moving_s"] for r in runs)
        if w.get("distance_km"):
            ratio = km / w["distance_km"]
        elif w.get("duration_min"):
            ratio = moving / 60 / w["duration_min"]
        else:
            ratio = 1.0
        if ratio < 0.8 and date.fromisoformat(w["date"]) >= today:
            # Today isn't over: a short morning run is the start of the day's training, not a "partial" workout, and must not block the day.
            out[w["id"]] = {"status": "planned", "actual_km": None, "activity_id": None}
            continue
        out[w["id"]] = {"status": "completed" if ratio >= 0.8 else "partial", "actual_km": round(km, 2), "activity_id": a["id"]}
    return out


def weekly_km(activities: list[dict]) -> dict[date, float]:
    totals: dict[date, float] = defaultdict(float)
    for a in activities:
        totals[monday(date.fromisoformat(a["date"]))] += a["distance_m"] / 1000
    return dict(totals)


def credit_plan(totals: dict[date, float], targets: dict[date, float] | None, tolerance: float = 0.9) -> dict[date, float]:
    """A week in which the athlete did at least `tolerance` of the plan's target counts as having done the target: delivered volume is a few
    percent under target by design (rounding, top-up limits), and re-basing on the delivered figure every Monday would shave that off again and
    again until the plan lagged well behind its own progression."""
    out = dict(totals)
    for ws, t in (targets or {}).items():
        a = totals.get(ws, 0.0)
        if t > 0 and a >= tolerance * t:
            out[ws] = max(a, t)
    return out


def recent_baseline_km(activities: list[dict], today: date, weeks: int = 4, fill_km: float = 0.0, targets: dict[date, float] | None = None) -> float | None:
    """Mean weekly km over the last `weeks` *complete* weeks, or None if there are no complete weeks of history.

    Weeks before the athlete's first recorded run are *unknown*, not zero (a runner who connected Strava two weeks ago hasn't stopped
    running before that): they count as `fill_km` (the stated weekly distance). Zeros after the first run are real. A planned step-back
    week (or one off week) among otherwise steady weeks is ignored, so a plan rebuilt every Monday doesn't forget its own progression.
    """
    if not activities:
        return None
    totals = credit_plan(weekly_km(activities), targets)
    this_wk = monday(today)
    first_wk = monday(min(date.fromisoformat(a["date"]) for a in activities))
    if first_wk >= this_wk:
        return None                                   # only this (incomplete) week so far
    vals = [totals.get(this_wk - timedelta(weeks=i), 0.0) if this_wk - timedelta(weeks=i) >= first_wk else fill_km for i in range(1, weeks + 1)]
    unknown = sum(1 for i in range(1, weeks + 1) if this_wk - timedelta(weeks=i) < first_wk)
    top = max(vals)
    if weeks >= 4 and unknown == 0 and top > 0 and sum(1 for v in vals if v >= 0.5 * top) >= weeks - 1:
        vals = sorted(vals, reverse=True)[:2]         # the two best weeks represent current capability; one step-back week doesn't count against it
    base = sum(vals) / len(vals)
    # The most recent complete week is the best evidence of where the athlete is NOW: after a break the older zero weeks would otherwise drag
    # the average down (a runner back to 25 km a week after two weeks off is not a 13 km runner), and a steadily building runner's mean lags.
    return max(base, totals.get(this_wk - timedelta(weeks=1), 0.0) if this_wk - timedelta(weeks=1) >= first_wk else 0.0)


def easy_pace_s_km(activities: list[dict], today: date, days: int = 28) -> float | None:
    """Median pace of recent runs >= 3km; a rough proxy for easy pace."""
    since = (today - timedelta(days=days)).isoformat()
    paces = [a["moving_s"] / (a["distance_m"] / 1000) for a in activities if a["date"] >= since and a["distance_m"] >= 3000]
    return median(paces) if len(paces) >= 3 else None
