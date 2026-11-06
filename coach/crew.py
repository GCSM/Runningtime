"""Crew: an opt-in, friends-only leaderboard of effort. Shares only first name, this week's distance and run count,
streak, and goal/countdown. Never routes, paces, heart rate or notes. You only see the crew if you are in it."""
from __future__ import annotations

from datetime import date, timedelta

from .db import DB
from .skeleton import monday

EMOJI = ("👏", "🔥", "💪")
OPT_KEY = "crew_optin"


def streak_of(db: DB, today: date) -> int:
    """Consecutive most-recent scheduled runs that were done (fully or partly), stopping at the first miss."""
    n = 0
    for w in reversed([w for w in db.get_workouts(end=today) if w["status"] in ("completed", "partial", "missed", "skipped")]):
        if w["status"] == "missed":
            break
        if w["status"] != "skipped":
            n += 1
    return n


def is_member(db: DB) -> bool:
    return bool(db.kv_get(OPT_KEY))


def join(db: DB) -> None:
    db.kv_set(OPT_KEY, True)


def leave(db: DB) -> None:
    db.kv_delete(OPT_KEY)


def card(root: DB, uid: int, today: date) -> dict | None:
    db, u = root.for_user(uid), root.user_by_id(uid)
    if not u:
        return None
    p = db.get_profile()
    wk = monday(today)
    acts = [a for a in db.activities_since(wk) if a["date"] <= (wk + timedelta(days=6)).isoformat()]
    units = p.units if p else "km"
    return {
        "user_id": uid, "first_name": (u["name"].split() or ["Runner"])[0][:24], "units": units,
        "week_km": round(sum(a["distance_m"] for a in acts) / 1000, 1), "runs": len(acts), "streak": streak_of(db, today),
        "goal": p.goal_label if p else "", "days_to_race": (p.race_date - today).days if p and p.race_date >= today else None,
    }


def crew(root: DB, viewer: int, today: date) -> list[dict]:
    """Everyone in the crew, ranked by this week's distance. Empty unless the viewer is a member."""
    if not is_member(root.for_user(viewer)):
        return []
    wk = monday(today).isoformat()
    ks = root.kudos_for_week(wk)
    out = []
    for uid in root.user_ids_with_kv(OPT_KEY, True):
        c = card(root, uid, today)
        if not c:
            continue
        c["me"] = uid == viewer
        c["kudos"] = [k["emoji"] for k in ks if k["to_user"] == uid]
        c["given"] = next((k["emoji"] for k in ks if k["from_user"] == viewer and k["to_user"] == uid), None)
        out.append(c)
    return sorted(out, key=lambda c: (-c["week_km"], c["first_name"].lower()))


def send_kudos(root: DB, viewer: int, to_user: int, emoji: str, today: date) -> None:
    from .errors import UserError
    if emoji not in EMOJI:
        raise UserError("Pick one of the reactions.")
    if viewer == to_user:
        raise UserError("You can't cheer for yourself (but nice try).")
    if not is_member(root.for_user(viewer)) or not is_member(root.for_user(to_user)):
        raise UserError("That runner isn't in your crew.")
    root.for_user(viewer).give_kudos(to_user, monday(today).isoformat(), emoji)
