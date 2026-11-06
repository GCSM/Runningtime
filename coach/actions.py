"""Things an athlete can do to their plan and their runs: skip, move, swap, ease off, give feedback.

All deterministic: no language model is involved, and every action is validated here so the chat feature (which can only
*propose* these) can't do anything the buttons couldn't.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import knowledge as K
from .errors import UserError
from .models import DAYS
from .plancheck import mentions_pain

FEELINGS = ("easy", "good", "hard", "pain")
EASE_OFF_DAYS = 7
PAIN_ADVICE = (
    "You flagged pain, so I've turned the next week's hard sessions into easy runs and trimmed the volume a little. "
    "Rest if it hurts while you run or walk. If it's sharp, swollen, or still there after a few days, please see a physio "
    "or doctor rather than running through it. Tell me when it feels better and I'll rebuild the plan from there."
)


def _day_name(d: date) -> str:
    return d.strftime("%A")


class ActionsMixin:
    """Mixed into CoachService (needs self.db, self.today, and _require_profile)."""

    # ---- helpers -------------------------------------------------------------------------------------
    def _movable(self, wid: int) -> dict:
        w = self.db.get_workout(wid)
        if not w:
            raise UserError("That workout wasn't found.")
        if w["type"] == "race":
            raise UserError("The race day itself can't be moved or skipped.")
        if w["status"] != "planned":
            raise UserError("Only upcoming workouts can be changed.")
        if date.fromisoformat(w["date"]) < self.today():
            raise UserError("That day has already passed.")
        return w

    def _occupied(self) -> dict[str, dict]:
        """date -> workout, for days that already hold a (non-skipped) workout or a completed run."""
        return {w["date"]: w for w in self.db.get_workouts(self.today()) if w["status"] != "skipped"}

    def move_options(self, wid: int, horizon_days: int = 14) -> list[dict]:
        """Days a workout could go to: free days (move) and occupied days (swap)."""
        w = self._movable(wid)
        p = self._require_profile()
        today, occ = self.today(), self._occupied()
        out = []
        last = min(today + timedelta(days=horizon_days), p.race_date - timedelta(days=1))
        d = today
        while d <= last:
            iso = d.isoformat()
            av = p.availability.get(DAYS[d.weekday()])
            other = occ.get(iso)
            if iso != w["date"] and not (other and other["type"] == "race"):
                out.append({"date": iso, "label": d.strftime("%a %b %-d"), "usual": bool(av and av.available),
                            "swap_with": other["id"] if other else None, "swap_title": other["title"] if other else None})
            d += timedelta(days=1)
        return out

    def _move_warnings(self, w: dict, new_date: date, ignore_ids: set[int] = frozenset()) -> list[str]:
        p = self._require_profile()
        warns = []
        av = p.availability.get(DAYS[new_date.weekday()])
        if not (av and av.available):
            warns.append(f"{_day_name(new_date)} isn't one of your usual running days.")
        elif av.minutes and (w["duration_min"] or 0) > av.minutes * 1.1:
            warns.append(f"This run ({w['duration_min']} min) is longer than the {av.minutes} minutes you listed for {_day_name(new_date)}.")
        hard = w["type"] in K.QUALITY_TYPES or w["type"] == "long"
        if hard:
            for o in self.db.get_workouts(new_date - timedelta(days=1), new_date + timedelta(days=1)):
                if o["id"] in ignore_ids or o["id"] == w["id"] or o["status"] == "skipped":
                    continue
                if o["type"] in K.QUALITY_TYPES or o["type"] == "long":
                    warns.append("That puts it right next to another hard session or the long run; an easy day between them recovers better.")
                    break
        return warns

    # ---- skip / move / swap -----------------------------------------------------------------------------
    def skip_workout(self, wid: int, reason: str = "") -> None:
        w = self._movable(wid)
        self.db.update_workout(wid, status="skipped", note=(reason or "").strip()[:200], actual_km=None, activity_id=None)
        self.db.log("skip", f"Skipped {w['title']} on {w['date']}." + (f" Reason: {reason.strip()[:200]}" if reason.strip() else ""))

    def restore_workout(self, wid: int) -> None:
        w = self.db.get_workout(wid)
        if not w or w["status"] != "skipped":
            raise UserError("That workout isn't skipped.")
        if date.fromisoformat(w["date"]) < self.today():
            raise UserError("That day has already passed.")
        self.db.update_workout(wid, status="planned", note="")

    def move_workout(self, wid: int, to: date) -> list[str]:
        w = self._movable(wid)
        p = self._require_profile()
        today = self.today()
        if to < today:
            raise UserError("Pick today or a later day.")
        if to >= p.race_date:
            raise UserError("Pick a day before the race.")
        occupied = self._occupied().get(to.isoformat())
        if occupied:
            raise UserError(f"{to.strftime('%A')} already has “{occupied['title']}”. Swap them instead.")
        warns = self._move_warnings(w, to)
        self.db.update_workout(wid, date=to.isoformat(), week_start=_monday(to).isoformat(),
                               moved_from=w["moved_from"] or w["date"])
        self.db.log("move", f"Moved {w['title']} from {w['date']} to {to.isoformat()}.")
        return warns

    def reschedule(self, wid: int, to: date) -> tuple[str, list[str]]:
        """Move a workout to a day; if that day already holds a workout, swap the two. Returns (what happened, warnings)."""
        other = self._occupied().get(to.isoformat())
        if other and other["id"] != wid and other["type"] != "race" and other["status"] == "planned":
            warns = self.swap_workouts(wid, other["id"])
            return f"Swapped with “{other['title']}” on {to.strftime('%A')}.", warns
        warns = self.move_workout(wid, to)
        return f"Moved to {to.strftime('%A %b %-d')}.", warns

    def swap_workouts(self, a_id: int, b_id: int) -> list[str]:
        if a_id == b_id:
            raise UserError("Pick two different workouts.")
        a, b = self._movable(a_id), self._movable(b_id)
        warns = self._move_warnings(a, date.fromisoformat(b["date"]), {b_id}) + self._move_warnings(b, date.fromisoformat(a["date"]), {a_id})
        da, db_ = date.fromisoformat(a["date"]), date.fromisoformat(b["date"])
        self.db.update_workout(a_id, date=b["date"], week_start=_monday(db_).isoformat(), moved_from=a["moved_from"] or a["date"])
        self.db.update_workout(b_id, date=a["date"], week_start=_monday(da).isoformat(), moved_from=b["moved_from"] or b["date"])
        self.db.log("move", f"Swapped {a['title']} ({a['date']}) with {b['title']} ({b['date']}).")
        return list(dict.fromkeys(warns))

    # ---- easing off (pain) ----------------------------------------------------------------------------------
    def ease_off(self, days: int = EASE_OFF_DAYS) -> int:
        """Turn the next `days` days of hard sessions into easy runs and trim volume. Returns how many workouts changed."""
        today = self.today()
        end = today + timedelta(days=days)
        changed = 0
        for w in self.db.get_workouts(today, end):
            if w["status"] != "planned" or w["type"] == "race":
                continue
            fields: dict = {}
            if w["type"] in K.QUALITY_TYPES:
                fields.update(type="easy", title="Easy run", description="Easy, conversational pace. Eased off after a pain report.",
                              pace_guidance="RPE 3–4: you could hold a conversation")
                factor = 0.85
            elif w["type"] == "long":
                factor = 0.75
            else:
                factor = 0.85
            if w["distance_km"]:
                fields["distance_km"] = round(w["distance_km"] * factor, 1)
                if w["duration_min"]:
                    fields["duration_min"] = int(round(w["duration_min"] * factor))
            fields["note"] = "Eased off after a pain report"
            self.db.update_workout(w["id"], **fields)
            changed += 1
        self.db.kv_set("pain_until", end.isoformat())
        return changed

    def pain_until(self) -> date | None:
        v = self.db.kv_get("pain_until")
        d = date.fromisoformat(v) if v else None
        return d if d and d > self.today() else None

    # ---- run feedback ---------------------------------------------------------------------------------------
    def pending_feedback(self) -> dict | None:
        """The most recent run from the last 3 days that the athlete hasn't rated yet."""
        today = self.today()
        fb = self.db.feedback_map()
        by_act = {w["activity_id"]: w for w in self.db.get_workouts(today - timedelta(days=3), today) if w["activity_id"]}
        for a in reversed(self.db.activities_since(today - timedelta(days=3))):
            if a["id"] not in fb:
                return {"activity": a, "workout": by_act.get(a["id"])}
        return None

    def save_feedback(self, activity_id: int, feeling: str, rpe: int | None = None, note: str = "") -> dict:
        if feeling not in FEELINGS:
            raise UserError("Pick how the run felt.")
        if self.db.get_activity(activity_id) is None:
            raise UserError("That run wasn't found.")
        if rpe is not None and not 1 <= int(rpe) <= 10:
            raise UserError("Effort is a number from 1 to 10.")
        note = (note or "").strip()[:300]
        self.db.save_feedback(activity_id, feeling, int(rpe) if rpe is not None else None, note)
        result = {"pain": False, "eased": 0}
        if feeling == "pain" or mentions_pain(note):
            result.update(pain=True, eased=self.ease_off())
            self.db.log("feedback", PAIN_ADVICE)
        return result

    def fatigue_signal(self) -> bool:
        """Two or more of the last four rated easy/long runs were hard (RPE ≥ 7 or marked hard)."""
        today = self.today()
        fb = self.db.feedback_map()
        recent = [w for w in self.db.get_workouts(today - timedelta(days=21), today)
                  if w["type"] in ("easy", "recovery", "long") and w["activity_id"] in fb]
        last = recent[-4:]
        hard = sum(1 for w in last if fb[w["activity_id"]]["feeling"] == "hard" or (fb[w["activity_id"]]["rpe"] or 0) >= 7)
        return hard >= 2


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())
