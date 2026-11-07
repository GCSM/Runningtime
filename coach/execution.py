"""Execution score: how closely a run matched what the plan asked for, in distance and in pace (0-100).

  distance score  100 when the distance is on target, falling 2 points per 1% off (so 10% off = 80, 25% off = 50, 50% off = 0), over or under.
  pace score      100 when the average pace sits inside the target band, falling to 0 at 15% outside it. Easy, long and recovery runs use the
                  athlete's easy-pace range (running them too fast costs points: easy days are meant to be easy). Quality sessions and races
                  use the plan's own average pace for the session (warm-up, jogs and cool-down included) with a tolerance.
  execution       the average of the two; distance alone when no pace target or no timing exists. A missed run scores 0 in the summary.

If an athlete is regularly under 70% over two weeks, `ease_suggestion` proposes a slightly easier plan. Scores are guidance, not grades:
GPS, hills, heat and weather all move pace, and the app says so.
"""
from __future__ import annotations

from datetime import date, timedelta

THRESHOLD = 70
MIN_RUNS = 4                    # "regularly": at least this many scored runs in the window
WINDOW_DAYS = 14
DIST_TOLERANCE = 0.50           # distance error at which the distance score reaches 0
PACE_TOLERANCE = 0.15           # distance outside the pace band (as a fraction of the pace) at which the pace score reaches 0
QUALITY_BAND = {"tempo": 0.05, "intervals": 0.08, "race": 0.04, "cross": 0.0}
EASY_TYPES = ("easy", "long", "recovery")
COOLDOWN_DAYS = 14


def fmt_pace(s: float) -> str:
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}/km"


def target_band(workout: dict, paces_s: dict | None) -> tuple[float, float] | None:
    """(fast, slow) average pace in s/km the run should fall inside, or None if there is nothing to compare against."""
    kind = workout.get("type")
    dist, dur = workout.get("distance_km") or 0, workout.get("duration_min") or 0
    implied = dur * 60 / dist if dist and dur else None
    if kind in EASY_TYPES:
        easy = (paces_s or {}).get("easy")
        if easy and len(easy) == 2:
            return float(easy[0]), float(easy[1])
        return (implied * 0.92, implied * 1.08) if implied else None
    tol = QUALITY_BAND.get(kind)
    if implied and tol:
        return implied * (1 - tol), implied * (1 + tol)
    return None


def _distance_score(planned_km: float, actual_km: float) -> float:
    if not planned_km:
        return 100.0
    return max(0.0, 100 * (1 - abs(actual_km / planned_km - 1) / DIST_TOLERANCE))


def _pace_score(pace: float, band: tuple[float, float]) -> tuple[float, float]:
    """(score, signed deviation outside the band as a fraction: negative = too fast, positive = too slow)."""
    lo, hi = band
    if pace < lo:
        dev = (pace - lo) / lo
    elif pace > hi:
        dev = (pace - hi) / hi
    else:
        return 100.0, 0.0
    return max(0.0, 100 * (1 - abs(dev) / PACE_TOLERANCE)), dev


def score_run(workout: dict, activity: dict | None, paces_s: dict | None) -> dict | None:
    """Score one completed or partial workout against its run. None when there is nothing to score (no run, skipped, race not done)."""
    if not activity or workout.get("status") not in ("completed", "partial") or not workout.get("distance_km"):
        return None
    actual_km = activity["distance_m"] / 1000
    if actual_km <= 0:
        return None
    d_score = _distance_score(workout["distance_km"], actual_km)
    band = target_band(workout, paces_s)
    moving = activity.get("moving_s") or 0
    pace = moving / actual_km if moving else None
    p_score, dev = (None, 0.0)
    if band and pace:
        p_score, dev = _pace_score(pace, band)
    score = round((d_score + p_score) / 2) if p_score is not None else round(d_score)
    notes = []
    off = actual_km / workout["distance_km"] - 1
    if abs(off) >= 0.05:
        notes.append(f"{abs(off):.0%} {'short' if off < 0 else 'long'}")
    if p_score is not None and abs(dev) >= 0.03:
        notes.append(f"pace {abs(dev):.0%} {'too fast' if dev < 0 else 'too slow'}")
    return {
        "score": score, "distance_score": round(d_score), "pace_score": None if p_score is None else round(p_score),
        "actual_km": round(actual_km, 1), "actual_pace": fmt_pace(pace) if pace else None,
        "target_pace": f"{fmt_pace(band[0])}–{fmt_pace(band[1])}" if band else None, "pace_dev": round(dev, 3),
        "label": label(score), "notes": notes, "headline": " · ".join(notes) if notes else "right on target",
    }


def label(score: int) -> str:
    return "Nailed it" if score >= 85 else "Solid" if score >= THRESHOLD else "Off target" if score >= 50 else "Way off"


def overview(rows: list[dict]) -> dict | None:
    """rows: [{'score': int | None (missed -> 0), 'missed': bool, 'dist': .., 'pace': .., 'dev': ..}] for the window. None if empty."""
    if not rows:
        return None
    scores = [r["score"] for r in rows]
    under = [r for r in rows if r["score"] < THRESHOLD]
    paced = [r for r in rows if r.get("pace_dev") is not None and not r["missed"]]
    too_fast = sum(1 for r in paced if r["pace_dev"] <= -0.03)
    short = sum(1 for r in rows if not r["missed"] and r.get("short"))
    missed = sum(1 for r in rows if r["missed"])
    reasons = sorted([("missed", missed), ("too_fast", too_fast), ("short", short)], key=lambda x: -x[1])
    return {"avg": round(sum(scores) / len(scores)), "n": len(rows), "under": len(under), "missed": missed, "too_fast": too_fast, "short": short,
            "main_reason": reasons[0][0] if reasons[0][1] else None}


REASON_TEXT = {
    "missed": "runs that were missed",
    "too_fast": "easy runs that were faster than the target pace",
    "short": "runs that finished well short of the planned distance",
}
