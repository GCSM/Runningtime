"""Fitness estimate, race prediction and trend from the athlete's own runs.

Honest by construction: the estimate is the best *recent effort* (VDOT of the fastest qualifying run in the last weeks).
Easy runs under-state fitness, so it is a floor that rises as harder efforts and races appear, and the app says so.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import knowledge as K
from .skeleton import monday

MIN_KM, MAX_KM, MIN_MIN = 3.0, 30.0, 12.0
VDOT_RANGE = (15.0, 85.0)          # outside this the run is almost certainly a GPS glitch or a ride
WINDOW_WEEKS = 6
DISTANCES = {"5K": 5000.0, "10K": 10000.0, "Half marathon": 21097.5, "Marathon": 42195.0}


def fmt_time(s: float) -> str:
    s = int(round(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def run_vdot(a: dict) -> float | None:
    km, minutes = a["distance_m"] / 1000, a["moving_s"] / 60
    if not (MIN_KM <= km <= MAX_KM) or minutes < MIN_MIN:
        return None
    v = K.vdot(a["distance_m"], a["moving_s"])
    return v if VDOT_RANGE[0] <= v <= VDOT_RANGE[1] else None


def _best(acts: list[dict], start: date, end: date) -> tuple[float, dict] | None:
    best = None
    for a in acts:
        d = date.fromisoformat(a["date"])
        if start <= d <= end:
            v = run_vdot(a)
            if v is not None and (best is None or v > best[0]):
                best = (v, a)
    return best


def estimate(acts: list[dict], today: date, weeks: int = WINDOW_WEEKS) -> dict | None:
    b = _best(acts, today - timedelta(weeks=weeks), today)
    if not b:
        return None
    v, a = b
    return {"vdot": round(v, 1), "activity_id": a["id"], "date": a["date"], "km": round(a["distance_m"] / 1000, 1)}


def trend(acts: list[dict], today: date, n_weeks: int = 12) -> list[dict]:
    """Best qualifying effort in the 6 weeks up to each of the last n weeks (None where there is nothing to go on)."""
    out = []
    for i in range(n_weeks - 1, -1, -1):
        end = monday(today) - timedelta(weeks=i) + timedelta(days=6)
        end = min(end, today)
        b = _best(acts, end - timedelta(weeks=WINDOW_WEEKS), end)
        out.append({"week_start": monday(end).isoformat(), "vdot": round(b[0], 1) if b else None})
    return out


def predictions(v_dot: float) -> dict[str, str]:
    return {name: fmt_time(K.race_time_s(v_dot, m)) for name, m in DISTANCES.items()}


def goal_assessment(v_dot: float, goal_km: float, goal_time_s: int | None) -> dict | None:
    if not goal_time_s:
        return None
    predicted = K.race_time_s(v_dot, goal_km * 1000)
    gap = (goal_time_s - predicted) / predicted * 100        # + = goal is slower than today's fitness supports
    if gap >= 2:
        verdict, tone = "Your current fitness already supports this goal.", "ahead"
    elif gap >= -2:
        verdict, tone = "Right on the edge of your current fitness. Consistent training should get you there.", "edge"
    elif gap >= -6:
        verdict, tone = "A stretch from where you are today, but realistic with a good build.", "stretch"
    else:
        verdict, tone = "Ambitious for now: it needs a big step up. The plan will tell you honestly if it still looks out of reach nearer race day.", "ambitious"
    return {"name": K.goal_name(goal_km), "predicted_s": round(predicted), "predicted": fmt_time(predicted), "goal": fmt_time(goal_time_s),
            "gap_pct": round(gap, 1),
            "verdict": verdict, "tone": tone}


def sparkline(series: list[dict], width: int = 240, height: int = 56, pad: int = 6) -> dict | None:
    pts = [(i, s["vdot"]) for i, s in enumerate(series) if s["vdot"] is not None]
    if len(pts) < 2:
        return None
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span = hi - lo or 1.0
    n = max(len(series) - 1, 1)
    xy = [(round(pad + i / n * (width - 2 * pad), 1), round(height - pad - (v - lo) / span * (height - 2 * pad), 1)) for i, v in pts]
    return {"d": "M" + " L".join(f"{x} {y}" for x, y in xy), "last": xy[-1], "width": width, "height": height, "lo": lo, "hi": hi}


def view(acts: list[dict], today: date, goal_km: float, goal_time_s: int | None) -> dict:
    est = estimate(acts, today)
    series = trend(acts, today)
    now = est["vdot"] if est else None
    earlier = next((s["vdot"] for s in series[:-4][::-1] if s["vdot"] is not None), None)    # roughly a month ago
    return {
        "estimate": est, "predictions": predictions(now) if now else None, "goal": goal_assessment(now, goal_km, goal_time_s) if now else None,
        "trend": series, "spark": sparkline(series), "delta": round(now - earlier, 1) if now and earlier else None,
    }
