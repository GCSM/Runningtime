"""Hard guard-rails applied to whatever the LLM returns, so a bad answer can't produce an unsafe, unschedulable or
under-delivering plan: only allowed days, no day overlap, time budgets, weekly volume caps and floors, and the coaching
rules researched in docs/RESEARCH.md (hard/easy spacing, quality-session limits, long-run caps and growth)."""
from __future__ import annotations

from datetime import date, timedelta

from . import knowledge as K
from .models import DAYS, WORKOUT_TYPES, Profile
from .skeleton import Week, monday

VOLUME_SLACK = 1.10  # allow 10% over the weekly target before scaling down
VOLUME_FLOOR = 0.85  # a week below this share of its target gets topped up (if the schedule allows)
TOP_UP_TO = 0.95     # …up to this share of the target
EASY_RUN_GROWTH = 1.5  # an easy run is never stretched beyond 1.5× what was planned
INJURY_WORDS = ("injur", "pain", "hurt", "sore", "strain", "sprain", "tight", "ache", "swollen", "stress fracture", "shin splint", "tendin", "niggle")


def mentions_pain(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in INJURY_WORDS)


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
    level: str = "intermediate",
    longest_recent_km: float = 0.0,
    suppress_quality_until: date | None = None,
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

    # ---- coaching rules (RESEARCH.md §3–5) ----------------------------------------------------------------
    run_days = sum(1 for a in profile.availability.values() if a.available)
    by_week: dict[str, list[dict]] = {}
    for w in sorted(clean, key=lambda w: w["date"]):
        by_week.setdefault(w["week_start"], []).append(w)

    def downgrade(w: dict, why: str) -> None:
        notes.append(f"{w['date']}: {w['type']} changed to an easy run ({why})")
        w.update(type="easy", title="Easy run", description="Easy, conversational pace.", pace_guidance="RPE 3–4: you could hold a conversation")

    for ws, items in by_week.items():
        wk = allowed_weeks.get(date.fromisoformat(ws))
        if wk is None:
            continue
        limit = K.quality_limit(level, wk.phase, wk.recovery, run_days)
        kept: list[dict] = []
        for w in items:
            if w["type"] not in K.QUALITY_TYPES:
                continue
            d = date.fromisoformat(w["date"])
            if suppress_quality_until and d < suppress_quality_until:
                downgrade(w, "you mentioned pain or injury, so no hard sessions for now"); continue
            if wk.recovery:
                downgrade(w, "recovery week"); continue
            near_long = any(o["type"] == "long" and abs((date.fromisoformat(o["date"]) - d).days) == 1 for o in items)
            if near_long:
                downgrade(w, "hard sessions need an easy day either side of the long run"); continue
            if any(abs((date.fromisoformat(k["date"]) - d).days) < 2 for k in kept):
                downgrade(w, "hard sessions need at least one easy day between them"); continue
            if len(kept) >= limit:
                downgrade(w, f"at most {limit} hard session{'s' * (limit != 1)} this week at your level and phase"); continue
            kept.append(w)

    # No single weekday run above its time cap: bigger weeks come from more run days, not from huge runs.
    for w in clean:
        if w["type"] in ("long", "race", "cross") or not w["distance_km"] or not w["duration_min"]:
            continue
        cap = K.run_cap_minutes(level, w["type"], profile.goal_km)
        if w["duration_min"] > cap + 1:
            new_km = round(w["distance_km"] * cap / w["duration_min"], 1)
            notes.append(f"{w['date']}: {w['type']} run shortened from {w['distance_km']:.1f} to {new_km:.1f} km (longer than {cap} minutes)")
            w["distance_km"], w["duration_min"] = new_km, cap

    # Hard-session volume: about half of a session is warm-up/cool-down, so cap session distance at 45% of the week
    # (true hard work then stays near the 80/20 guideline). Easy runs are topped up below to restore the week's volume.
    for ws, items in by_week.items():
        wk = allowed_weeks.get(date.fromisoformat(ws))
        q = [w for w in items if w["type"] in K.QUALITY_TYPES]
        q_km = sum(w["distance_km"] for w in q)
        if wk is None or not q or q_km <= K.HARD_SHARE_MAX * wk.target_km + 0.05:
            continue
        s = K.HARD_SHARE_MAX * wk.target_km / q_km
        for w in q:
            new = max(round(w["distance_km"] * s, 1), min(3.0, w["distance_km"]))
            w["duration_min"] = int(round(w["duration_min"] * new / w["distance_km"])) if w["distance_km"] else w["duration_min"]
            w["distance_km"] = new
        notes.append(f"week of {ws}: hard sessions shortened to stay within {K.HARD_SHARE_MAX:.0%} of the week's distance")

    # Long runs: capped by size, time, and growth over the previous 4 weeks.
    cap_km = K.long_run_cap_km(profile.goal_km, level)
    cap_min = K.long_run_max_minutes(profile.goal_km)
    history: list[tuple[date, float]] = [(today - timedelta(days=1), longest_recent_km)] if longest_recent_km > 0 else []
    for w in sorted((x for x in clean if x["type"] == "long"), key=lambda x: x["date"]):
        d = date.fromisoformat(w["date"])
        wk = allowed_weeks.get(monday(d))
        limits = [cap_km * 1.05]
        if wk and wk.long_run_km > 0:
            limits.append(wk.long_run_km * 1.10)
        if w["duration_min"] > cap_min:
            limits.append(w["distance_km"] * cap_min / w["duration_min"])
        prior = max((km for dd, km in history if 0 < (d - dd).days <= 28), default=0.0)
        if prior > 0:
            limits.append(max(prior * (1 + K.LONG_RUN_GROWTH_PCT), prior + K.LONG_RUN_GROWTH_FLOOR_KM))
        allowed = min(limits)
        if w["distance_km"] > allowed + 0.05:
            s = allowed / w["distance_km"]
            notes.append(f"{w['date']}: long run reduced from {w['distance_km']:.1f} to {allowed:.1f} km (size, time or growth limit)")
            w["distance_km"], w["duration_min"] = round(allowed, 1), int(round(w["duration_min"] * s))
        history.append((d, w["distance_km"]))

    # Under-delivering weeks: top easy runs up toward the target, within each day's time and a growth limit.
    for ws, items in by_week.items():
        wk = allowed_weeks.get(date.fromisoformat(ws))
        if wk is None or wk.race_week:
            continue
        done = done_km_by_week.get(date.fromisoformat(ws), 0.0)
        total = done + sum(w["distance_km"] for w in items)
        if total >= wk.target_km * VOLUME_FLOOR:
            continue
        easy = [w for w in items if w["type"] in ("easy", "recovery")]
        long_km = max((w["distance_km"] for w in items if w["type"] == "long"), default=0.0)

        def headroom(growth: float | None) -> dict[int, float]:
            """Extra km each easy run can take: within that day's minutes, never longer than the long run, and (first
            pass) within a growth limit so the model's own structure is respected."""
            out = {}
            for w in easy:
                av = profile.availability.get(DAYS[date.fromisoformat(w["date"]).weekday()])
                max_min = min(av.minutes if av and av.minutes else w["duration_min"], K.run_cap_minutes(level, w["type"], profile.goal_km))
                km_per_min = w["distance_km"] / w["duration_min"] if w["duration_min"] else 60 / pace_s_km
                ceiling = min(max_min * km_per_min, long_km * 0.85 if long_km else 1e9)
                if growth:
                    ceiling = min(ceiling, w["distance_km"] * growth)
                out[id(w)] = max(0.0, ceiling - w["distance_km"])
            return out

        def spread(need: float, room: dict[int, float]) -> None:
            avail = sum(room.values())
            if need <= 0 or avail <= 0:
                return
            share = min(1.0, need / avail)
            for w in easy:
                add = room[id(w)] * share
                if add > 0.05 and w["distance_km"]:
                    f = (w["distance_km"] + add) / w["distance_km"]
                    w["distance_km"] = round(w["distance_km"] + add, 1)
                    w["duration_min"] = int(round(w["duration_min"] * f))

        goal = wk.target_km * TOP_UP_TO
        spread(goal - total, headroom(EASY_RUN_GROWTH))
        now = done + sum(w["distance_km"] for w in items)
        if now < wk.target_km * VOLUME_FLOOR:               # still short: use the time the athlete has actually given us
            spread(goal - now, headroom(None))
            now = done + sum(w["distance_km"] for w in items)
        if now > total + 0.05:
            notes.append(f"week of {ws}: easy runs lengthened so the week reaches {now:.0f} of {wk.target_km:.0f} km")
        if now < wk.target_km * VOLUME_FLOOR:
            notes.append(f"week of {ws}: {now:.0f} km vs a {wk.target_km:.0f} km target; your available days/minutes limit it")

    # Weekly volume cap: scale non-race runs down proportionally.
    for ws, wk in allowed_weeks.items():
        in_week = [w for w in clean if w["week_start"] == ws.isoformat()]
        race_km = sum(w["distance_km"] for w in in_week if w["type"] == "race")
        runs = [w for w in in_week if w["type"] != "race"]
        total = sum(w["distance_km"] for w in runs)
        room = wk.target_km * VOLUME_SLACK - race_km - done_km_by_week.get(ws, 0.0)
        if runs and total > room:
            # Scale to fit exactly. Only the current week keeps a floor (some of it is already behind the athlete).
            floor = 0.4 if done_km_by_week.get(ws, 0.0) > 0 or room <= 0 else 0.0
            s = max(room / total, floor)
            notes.append(f"week of {ws}: scaled {total:.0f}km to fit {wk.target_km:.0f}km target")
            for w in runs:
                w["distance_km"] = round(w["distance_km"] * s, 1)
                w["duration_min"] = int(round(w["duration_min"] * s))
    clean.sort(key=lambda w: w["date"])
    return clean, notes
