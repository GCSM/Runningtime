"""Hard guard-rails applied to whatever the LLM returns, so a bad answer can't produce an unsafe or
unschedulable plan: only allowed days, no day overlap, respect time budgets and weekly volume caps."""
from __future__ import annotations

from datetime import date, timedelta

from .models import DAYS, WORKOUT_TYPES, Profile
from .skeleton import Week, monday

VOLUME_SLACK = 1.10  # allow 10% over the weekly target before scaling down


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def sanitize(
    raw: list[dict],
    *,
    profile: Profile,
    weeks: list[Week],
    today: date,
    blocked_dates: set[str],
    done_km_by_week: dict[date, float],
    pace_s_km: float,
) -> tuple[list[dict], list[str]]:
    notes: list[str] = []
    allowed_weeks = {w.week_start: w for w in weeks}
    seen: set[str] = set()
    clean: list[dict] = []
    for r in raw:
        try:
            d = date.fromisoformat(str(r.get("date")))
        except ValueError:
            notes.append(f"dropped workout with invalid date {r.get('date')!r}")
            continue
        iso = d.isoformat()
        wtype = r.get("type") if r.get("type") in WORKOUT_TYPES else "easy"
        is_race = d == profile.race_date
        if d < today or d > profile.race_date or monday(d) not in allowed_weeks:
            notes.append(f"{iso}: outside the planning window")
            continue
        if iso in blocked_dates or iso in seen:
            notes.append(f"{iso}: day already taken")
            continue
        av = profile.availability.get(DAYS[d.weekday()])
        if not is_race and not (av and av.available):
            notes.append(f"{iso}: not an available running day")
            continue
        if is_race:
            wtype = "race"
        elif wtype == "race":
            wtype = "easy"
        dist = _f(r.get("distance_km"))
        dur = _f(r.get("duration_min"))
        if dist is None and dur is None:
            notes.append(f"{iso}: no distance or duration")
            continue
        if dist is None:
            dist = dur * 60 / pace_s_km if wtype != "cross" else 0.0
        if dur is None:
            dur = dist * pace_s_km / 60
        if not is_race and av and av.minutes and dur > av.minutes * 1.1:
            s = av.minutes / dur
            notes.append(f"{iso}: scaled to fit {av.minutes} min available")
            dist, dur = dist * s, dur * s
        seen.add(iso)
        clean.append({
            "date": iso, "week_start": monday(d).isoformat(), "type": wtype,
            "title": str(r.get("title") or wtype.title())[:120],
            "description": str(r.get("description") or ""),
            "distance_km": round(max(dist, 0), 1), "duration_min": int(round(max(dur, 0))),
            "pace_guidance": str(r.get("pace_guidance") or ""),
        })

    # The race itself must exist.
    if profile.race_date >= today and profile.race_date.isoformat() not in seen and profile.race_date.isoformat() not in blocked_dates:
        dur = profile.goal_km * (profile.goal_pace_s_per_km or pace_s_km) / 60
        clean.append({
            "date": profile.race_date.isoformat(), "week_start": monday(profile.race_date).isoformat(),
            "type": "race", "title": f"Race day: {profile.goal_label}", "description": "Go get it.",
            "distance_km": round(profile.goal_km, 1), "duration_min": int(round(dur)), "pace_guidance": "",
        })
        notes.append("added missing race-day workout")

    # Weekly volume cap: scale non-race runs down proportionally.
    for ws, wk in allowed_weeks.items():
        in_week = [w for w in clean if w["week_start"] == ws.isoformat()]
        race_km = sum(w["distance_km"] for w in in_week if w["type"] == "race")
        runs = [w for w in in_week if w["type"] != "race"]
        total = sum(w["distance_km"] for w in runs)
        room = wk.target_km * VOLUME_SLACK - race_km - done_km_by_week.get(ws, 0.0)
        if runs and total > room:
            s = max(room / total, 0.4)
            notes.append(f"week of {ws}: scaled {total:.0f}km to fit {wk.target_km:.0f}km target")
            for w in runs:
                w["distance_km"] = round(w["distance_km"] * s, 1)
                w["duration_min"] = int(round(w["duration_min"] * s))
    clean.sort(key=lambda w: w["date"])
    return clean, notes
