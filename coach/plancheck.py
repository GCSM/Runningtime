"""Hard guard-rails applied to whatever the LLM returns, so a bad answer can't produce an unsafe, unschedulable or
under-delivering plan: only allowed days, no day overlap, time budgets, weekly volume caps and floors, and the coaching
rules researched in docs/RESEARCH.md (hard/easy spacing, quality-session limits, long-run caps and growth)."""
from __future__ import annotations

import math
import re
from datetime import date, timedelta

from . import knowledge as K
from .models import DAYS, WORKOUT_TYPES, Profile
from .skeleton import Week, monday

VOLUME_SLACK = 1.10  # allow 10% over the weekly target before scaling down
VOLUME_FLOOR = 0.85  # a week below this share of its target gets topped up (if the schedule allows)
TOP_UP_TO = 0.95     # …up to this share of the target
EASY_RUN_GROWTH = 1.5  # an easy run is never stretched beyond 1.5× what was planned
_BODY = r"(?:knee|ankle|achilles|calf|calves|shin|hip|hamstring|quad|quads|thigh|foot|feet|heel|back|groin|glute|it band|itb|arch|toe|hip flexor|plantar)"
_PAIN = re.compile(
    r"\b(?:injur(?:y|ed|ies)|pain(?:ful)?|hurts?|hurting|sore(?:ness)?|strain(?:ed)?|sprain(?:ed)?|swollen|swelling|stress fracture|shin splints?|tendin(?:itis|opathy)|niggles?|limp(?:ing)?)\b"
    rf"|\b(?:tight|ache|aches|aching)\b(?=[^.;,]{{0,25}}\b{_BODY}\b)|\b{_BODY}\b[^.;,]{{0,25}}\b(?:tight|aches?|aching)\b", re.I)
_NEGATED = re.compile(r"\b(?:no|not|never|without|zero|free of|nothing|isn'?t|aren'?t|wasn'?t|weren'?t|don'?t|doesn'?t|didn'?t)\b[^.;!?]{0,18}$", re.I)
_PAIN_FREE = re.compile(r"\b(?:pain|injury)[- ]free\b", re.I)


def mentions_pain(text: str) -> bool:
    """Does an athlete's note report pain or injury? Whole words only (not "painting", "headache", "tight schedule"), and "no pain" or
    "pain-free" don't count."""
    t = _PAIN_FREE.sub("", text or "")
    for m in _PAIN.finditer(t):
        if not _NEGATED.search(t[max(0, m.start() - 30):m.start()]):
            return True
    return False


def _f(v, default=None):
    """A finite number from whatever the model sent, else `default`. Booleans, NaN and infinity are not numbers."""
    if isinstance(v, bool):
        return default
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


_PACED = re.compile(r"\b(?:finish|finishing|progression|progressive|marathon[- ]pace|half[- ]marathon[- ]pace|hmp|mp|goal[- ]pace|tempo|threshold)\b", re.I)


def is_paced_long(w: dict) -> bool:
    """A long run that includes a pace finish is a hard effort, not an easy long run."""
    return w.get("type") == "long" and bool(_PACED.search(f"{w.get('title', '')} {w.get('description', '')} {w.get('pace_guidance', '')}"))


_KM_IN_TEXT = re.compile(r"(\d+(?:\.\d+)?)(\s*km)\b")


def retitle(title: str, old_km: float, new_km: float) -> str:
    """Update a distance mentioned in a title ("Easy 15 km") after the workout was shortened or lengthened; other titles are left alone."""
    def fix(m: re.Match) -> str:
        return f"{new_km:g}{m.group(2)}" if abs(float(m.group(1)) - old_km) <= max(0.3, 0.05 * old_km) else m.group(0)
    return _KM_IN_TEXT.sub(fix, title)


def _reconcile_text(clean: list[dict]) -> None:
    """Keep what the athlete READS consistent with what we SAVED. When a distance was changed (scaled to fit the week, the day's minutes or a
    cap), a title like "Tempo 12 km" or a session structure in the description would otherwise describe a different workout."""
    for w in clean:
        orig = w.pop("_orig_km", None)
        new = w["distance_km"]
        if not orig or abs(new - orig) <= max(0.3, 0.05 * orig) or w["type"] in ("race", "cross"):
            continue
        retitled = [False]

        def fix(m: re.Match) -> str:
            if abs(float(m.group(1)) - orig) <= max(0.3, 0.05 * orig):
                retitled[0] = True
                return f"{new:g}{m.group(2)}"
            return m.group(0)

        w["title"] = _KM_IN_TEXT.sub(fix, w["title"])
        if w["type"] in K.QUALITY_TYPES or (w["type"] == "long" and not retitled[0]):
            w["description"] = (w["description"] + " " if w["description"] else "") + (
                f"(Adjusted from {orig:g} km to {new:g} km to fit your week: do fewer repeats or a shorter warm-up/cool-down, keeping the same effort.)")


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
    done_quality: dict[date, int] | None = None,
) -> tuple[list[dict], list[str]]:
    notes: list[str] = []
    done_quality = done_quality or {}
    allowed_weeks = {w.week_start: w for w in weeks}
    seen: set[str] = set()
    clean: list[dict] = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            notes.append("dropped an entry that wasn't a workout")
            continue
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
        if dist is not None and dist <= 0 and wtype != "cross":
            dist = None                                   # "0 km" or a negative distance is no distance
        if dur is not None and dur <= 0:
            dur = None
        if dist is None and dur is None and not is_race:
            notes.append(f"{iso}: no usable distance or duration")
            continue
        if dist is None and dur is not None:
            dist = dur * 60 / pace_s_km if wtype != "cross" else 0.0
        if dur is None and dist is not None:
            dur = dist * pace_s_km / 60
        model_km = dist                                        # what the model asked for, before any correction (titles/descriptions are reconciled to it)
        if is_race:
            # The race is the goal distance, whatever the model wrote (a wrong figure would also distort race week's volume).
            dist = profile.goal_km
            dur = (profile.goal_time_s / 60) if profile.goal_time_s else dist * pace_s_km / 60
        elif wtype != "cross" and dist and dur:
            implied = dur * 60 / dist
            lo, hi = (0.55, 1.35) if wtype in K.QUALITY_TYPES else (0.7, 1.8)
            if not (lo * pace_s_km <= implied <= hi * pace_s_km):
                fixed = dist * pace_s_km / 60                  # keep the distance (it drives the week's volume), recompute the time
                notes.append(f"{iso}: {dist:.1f} km in {dur:.0f} min isn't a believable pace; time corrected to {fixed:.0f} min")
                dur = fixed
        if not is_race and av and av.minutes and dur > av.minutes + 1:
            s_ = av.minutes / dur
            notes.append(f"{iso}: scaled to fit {av.minutes} min available")
            dist, dur = dist * s_, dur * s_
        seen.add(iso)
        clean.append({
            "date": iso, "week_start": monday(d).isoformat(), "type": wtype,
            "title": str(r.get("title") or wtype.title())[:120],
            "description": str(r.get("description") or ""),
            "distance_km": round(max(dist, 0), 1), "duration_min": int(round(max(dur, 0))),
            "pace_guidance": str(r.get("pace_guidance") or ""), "_orig_km": round(max(model_km or 0.0, 0), 1),
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

    all_sorted = sorted(clean, key=lambda w: w["date"])
    # The long run lives on the athlete's long-run day, once a week, and not in the last days before the race.
    for ws, items in by_week.items():
        longs = [w for w in items if w["type"] == "long"]
        for w in longs:
            d = date.fromisoformat(w["date"])
            if DAYS[d.weekday()] != profile.long_run_day:
                downgrade(w, f"your long run belongs on {profile.long_run_day.title()}"); continue
            if 0 <= (profile.race_date - d).days <= 5:
                downgrade(w, "no long run in the last days before the race"); continue
        longs = [w for w in items if w["type"] == "long"]
        for extra in sorted(longs, key=lambda w: -w["distance_km"])[1:]:
            downgrade(extra, "one long run a week")

    kept_all: list[dict] = []                              # hard sessions kept so far, across weeks (spacing doesn't reset on Monday)
    for ws, items in by_week.items():
        wk = allowed_weeks.get(date.fromisoformat(ws))
        if wk is None:
            continue
        limit = K.quality_limit(level, wk.phase, wk.recovery, run_days) - done_quality.get(date.fromisoformat(ws), 0)
        if limit >= 2 and any(is_paced_long(w) for w in items):
            limit -= 1                                     # a long run with a pace finish already is one of the week's hard efforts
        kept_this_week = 0
        for w in items:
            if w["type"] not in K.QUALITY_TYPES:
                continue
            d = date.fromisoformat(w["date"])
            if suppress_quality_until and d < suppress_quality_until:
                downgrade(w, "you mentioned pain or injury, so no hard sessions for now"); continue
            if wk.recovery:
                downgrade(w, "recovery week"); continue
            if 0 <= (profile.race_date - d).days <= 2:
                downgrade(w, "keep the last days before the race easy"); continue
            near_long = any(o["type"] == "long" and abs((date.fromisoformat(o["date"]) - d).days) == 1 for o in all_sorted)
            if near_long:
                downgrade(w, "hard sessions need an easy day either side of the long run"); continue
            if any(abs((date.fromisoformat(k["date"]) - d).days) < 2 for k in kept_all):
                downgrade(w, "hard sessions need at least one easy day between them"); continue
            if kept_this_week >= max(limit, 0):
                downgrade(w, f"at most {K.quality_limit(level, wk.phase, wk.recovery, run_days)} hard session"
                             f"{'s' * (K.quality_limit(level, wk.phase, wk.recovery, run_days) != 1)} this week at your level and phase"); continue
            kept_this_week += 1
            kept_all.append(w)

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
        if runs and total > 0 and total > room:
            # Scale to fit exactly. Only the current week keeps a floor (some of it is already behind the athlete).
            floor = 0.4 if done_km_by_week.get(ws, 0.0) > 0 or room <= 0 else 0.0
            s = max(room / total, floor)
            notes.append(f"week of {ws}: scaled {total:.0f}km to fit {wk.target_km:.0f}km target")
            for w in runs:
                w["distance_km"] = round(w["distance_km"] * s, 1)
                w["duration_min"] = int(round(w["duration_min"] * s))
    _reconcile_text(clean)
    clean.sort(key=lambda w: w["date"])
    return clean, notes
