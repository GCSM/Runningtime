from __future__ import annotations

import threading
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import adherence
from .config import Settings
from .db import DB
from .models import DAYS, Profile
from .plancheck import sanitize
from .skeleton import DEFAULT_EASY_PACE_S_KM, Week, build_skeleton, min_recommended_weeks, monday

_LOCKS: dict[tuple, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
HISTORY_DAYS = 90
FIX_RETRY_RATIO = 0.25


class UserError(Exception):
    """A problem the user can fix; shown verbatim in the UI."""


class CoachService:
    def __init__(self, db: DB, settings: Settings, coach, strava, clock=None):
        self.db, self.settings, self.coach, self.strava = db, settings, coach, strava
        self.today = clock or settings.today
        with _LOCKS_GUARD:  # one LLM/sync operation at a time *per user*
            self._lock = _LOCKS.setdefault((db.path, db.uid), threading.Lock())

    # ---- Strava ----------------------------------------------------------
    def sync(self) -> int:
        if not self.strava.connected:
            raise UserError("Connect Strava first.")
        last = self.db.latest_activity_date()
        since = (last - timedelta(days=7)) if last else self.today() - timedelta(days=HISTORY_DAYS)
        ts = int(datetime.combine(since, dtime.min, tzinfo=timezone.utc).timestamp())
        new = self.db.upsert_activities(self.strava.fetch_runs(ts))
        self.refresh_statuses()
        self.db.kv_set("last_sync", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return new

    def refresh_statuses(self) -> None:
        today = self.today()
        ws = self.db.get_workouts(end=today + timedelta(days=1))
        if not ws:
            return
        acts = self.db.activities_since(date.fromisoformat(ws[0]["date"]))
        for wid, m in adherence.match_workouts(ws, acts, today).items():
            self.db.update_workout_status(wid, m["status"], m["actual_km"], m["activity_id"])

    # ---- plan generation / review ------------------------------------------
    def _require_profile(self) -> Profile:
        p = self.db.get_profile()
        if not p:
            raise UserError("Set up your goal and schedule first.")
        if p.race_date < self.today() + timedelta(days=3):
            raise UserError("The race date is too close (or past). Update your goal.")
        return p

    def _skeleton(self, p: Profile, acts: list[dict], plan_start: date) -> tuple[list[Week], float]:
        today = self.today()
        pace = adherence.easy_pace_s_km(acts, today)
        base = adherence.recent_baseline_km(acts, today)
        weeks = build_skeleton(
            plan_start=plan_start, today=today, race_date=p.race_date, goal_km=p.goal_km,
            baseline_km=p.baseline_km if base is None else base, experience=p.experience,
            weekly_minutes=p.weekly_minutes, long_day_minutes=p.long_day_minutes, easy_pace_s_km=pace,
        )
        return weeks, pace or DEFAULT_EASY_PACE_S_KM

    def _context(self, p: Profile, weeks: list[Week], acts: list[dict], pace: float, plan_start: date) -> dict:
        today = self.today()
        totals = adherence.weekly_km(acts)
        done_runs = {w["date"] for w in self.db.get_workouts(today, today) if w["status"] in ("completed", "partial")}
        blocked = sorted(done_runs | ({today.isoformat()} if any(a["date"] == today.isoformat() for a in acts) else set()))
        ctx = {
            "today": today.isoformat(), "today_weekday": DAYS[today.weekday()],
            "earliest_workout_date": today.isoformat(), "blocked_dates": blocked,
            "athlete": {
                "name": p.name, "experience": p.experience, "notes": p.notes,
                "goal": p.goal_label, "goal_km": p.goal_km, "race_date": p.race_date.isoformat(),
                "goal_time": _fmt_hms(p.goal_time_s) if p.goal_time_s else None,
                "goal_pace_s_per_km": round(p.goal_pace_s_per_km) if p.goal_pace_s_per_km else None,
                "weeks_until_race": (p.race_date - today).days // 7,
            },
            "availability": {d: p.availability[d].model_dump() for d in DAYS if d in p.availability},
            "long_run_day": p.long_run_day,
            "recent_training": {
                "weekly_km_last_6_complete_weeks": [round(totals.get(monday(today) - timedelta(weeks=i), 0.0), 1) for i in range(6, 0, -1)],
                "easy_pace_s_per_km_estimate": round(pace),
                "have_strava_history": bool(acts),
            },
            "skeleton": [
                {**{k: v for k, v in w.to_row().items() if k != "week_start"}, "week_start": w.week_start.isoformat(),
                 "already_done_km": round(totals.get(w.week_start, 0.0), 1) if w.week_start == monday(today) else 0.0}
                for w in weeks
            ],
        }
        return ctx

    def _run_coach(self, p, weeks, acts, pace, ctx, mode) -> tuple[list[dict], str]:
        today = self.today()
        totals = adherence.weekly_km(acts)
        kwargs = dict(
            profile=p, weeks=weeks, today=today, blocked_dates=set(ctx["blocked_dates"]),
            done_km_by_week={monday(today): totals.get(monday(today), 0.0)}, pace_s_km=pace,
        )
        out = self.coach.plan(ctx, mode=mode)
        workouts, notes = sanitize(out["workouts"], **kwargs)
        if notes and len(notes) > max(2, FIX_RETRY_RATIO * len(out["workouts"])):
            out = self.coach.plan(ctx, mode=mode, feedback=notes)
            workouts, notes = sanitize(out["workouts"], **kwargs)
        if not workouts:
            raise UserError("The coach returned no usable workouts. Check your availability and try again.")
        self.db.replace_plan_from(self.today(), [w.to_row() for w in weeks if w.week_start >= monday(today)], workouts)
        self.refresh_statuses()
        return workouts, out["summary"]

    def generate_plan(self) -> str:
        with self._lock:
            p = self._require_profile()
            today = self.today()
            acts = self.db.activities_since(today - timedelta(days=HISTORY_DAYS))
            plan_start = monday(today)
            self.db.kv_set("plan_start", plan_start.isoformat())
            weeks, pace = self._skeleton(p, acts, plan_start)
            ctx = self._context(p, weeks, acts, pace, plan_start)
            _, summary = self._run_coach(p, weeks, acts, pace, ctx, "generate")
            short = (p.race_date - today).days // 7 < min_recommended_weeks(p.goal_km)
            if short:
                summary += f"\n\nHeads-up: {min_recommended_weeks(p.goal_km)}+ weeks is ideal for this distance, so this is a compressed build."
            self.db.log("plan", summary)
            self.db.kv_set("last_review_week", monday(today).isoformat())
            return summary

    def review(self, note: str = "", sync: bool = True) -> str:
        with self._lock:
            p = self._require_profile()
            if not self.db.get_workouts():
                raise UserError("Generate a plan first.")
            if sync and self.strava.connected:
                self.sync()
            else:
                self.refresh_statuses()
            today = self.today()
            acts = self.db.activities_since(today - timedelta(days=HISTORY_DAYS))
            plan_start = date.fromisoformat(self.db.kv_get("plan_start", monday(today).isoformat()))
            weeks, pace = self._skeleton(p, acts, plan_start)
            ctx = self._context(p, weeks, acts, pace, plan_start)
            ctx["athlete_note"] = note
            ctx["last_4_weeks_review"] = self._review_block(acts)
            ctx["unplanned_runs_last_14_days"] = self._extra_runs(acts)
            ctx["current_upcoming_plan"] = [
                {k: w[k] for k in ("date", "type", "title", "distance_km")} for w in self.db.get_workouts(today, today + timedelta(days=13))
                if w["status"] == "planned"
            ]
            _, summary = self._run_coach(p, weeks, acts, pace, ctx, "adjust")
            self.db.log("review", summary if not note else f"You said: {note}\n\n{summary}")
            self.db.kv_set("last_review_week", monday(today).isoformat())
            return summary

    def _review_block(self, acts: list[dict]) -> list[dict]:
        today = self.today()
        by_id = {a["id"]: a for a in acts}
        out = []
        for i in range(4, -1, -1):
            ws = monday(today) - timedelta(weeks=i)
            planned = self.db.get_workouts(ws, ws + timedelta(days=6))
            if not planned and i:
                continue
            runs = []
            for w in planned:
                a = by_id.get(w["activity_id"])
                runs.append({
                    "date": w["date"], "type": w["type"], "planned_km": w["distance_km"], "status": w["status"],
                    "actual_km": w["actual_km"],
                    "avg_pace_s_per_km": round(a["moving_s"] / (a["distance_m"] / 1000)) if a else None,
                    "avg_hr": a["avg_hr"] if a else None,
                })
            out.append({
                "week_start": ws.isoformat(), "planned_km": round(sum(w["distance_km"] or 0 for w in planned), 1),
                "actual_km_all_runs": round(adherence.weekly_km(acts).get(ws, 0.0), 1), "workouts": runs,
            })
        return out

    def _extra_runs(self, acts: list[dict]) -> list[dict]:
        today = self.today()
        matched = {w["activity_id"] for w in self.db.get_workouts(today - timedelta(days=14), today) if w["activity_id"]}
        return [
            {"date": a["date"], "km": round(a["distance_m"] / 1000, 1), "pace_s_per_km": round(a["moving_s"] / (a["distance_m"] / 1000)), "avg_hr": a["avg_hr"]}
            for a in acts if a["date"] >= (today - timedelta(days=14)).isoformat() and a["id"] not in matched
        ]

    # ---- scheduler hook --------------------------------------------------
    def background_tick(self) -> None:
        """Sync regularly; auto-review once at the start of each new week."""
        if not self.strava.connected:
            return
        self.sync()
        if (
            self.settings.auto_adjust
            and self.db.get_workouts()
            and self.db.kv_get("last_review_week") != monday(self.today()).isoformat()
            and self.today() < (self.db.get_profile().race_date if self.db.get_profile() else date.min)
        ):
            self.review(sync=False)

    # ---- views -----------------------------------------------------------
    def dashboard(self) -> dict:
        today = self.today()
        p = self.db.get_profile()
        data: dict = {"profile": p, "today": today, "strava_connected": self.strava.connected,
                      "strava_name": self.strava.athlete_name() if self.strava.connected else "",
                      "last_sync": self.db.kv_get("last_sync"), "log": self.db.recent_log(1)}
        if not p:
            return data
        data["days_to_race"] = (p.race_date - today).days
        ws = monday(today)
        by_date = {w["date"]: w for w in self.db.get_workouts(ws, ws + timedelta(days=6))}
        data["strip"] = [
            {"date": (ws + timedelta(days=i)), "label": DAYS[i][0].upper(), "workout": by_date.get((ws + timedelta(days=i)).isoformat()),
             "is_today": ws + timedelta(days=i) == today}
            for i in range(7)
        ]
        data["today_workout"] = by_date.get(today.isoformat())
        data["week_workouts"] = list(by_date.values())
        week = {w["week_start"]: w for w in self.db.get_weeks()}.get(ws.isoformat())
        actual = round(adherence.weekly_km(self.db.activities_since(ws)).get(ws, 0.0), 1)
        data["week"], data["week_actual_km"] = week, actual
        data["week_pct"] = min(100, round(100 * actual / week["target_km"])) if week and week["target_km"] else 0
        data["has_plan"] = bool(self.db.get_workouts())
        data["next_workout"] = next((w for w in self.db.get_workouts(today + timedelta(days=1)) if w["status"] == "planned"), None)
        data["streak"] = self.streak()
        data["journey"] = self.journey()
        return data

    def streak(self) -> int:
        """Consecutive most-recent scheduled runs that were done (fully or partly), stopping at the first miss."""
        past = [w for w in self.db.get_workouts(end=self.today()) if w["status"] in ("completed", "partial", "missed")]
        n = 0
        for w in reversed(past):
            if w["status"] == "missed":
                break
            n += 1
        return n

    def journey(self) -> dict | None:
        """The whole plan as phase segments plus where the athlete is now."""
        weeks = self.db.get_weeks()
        if not weeks:
            return None
        this_wk = monday(self.today()).isoformat()
        segs: list[dict] = []
        for w in weeks:
            if segs and segs[-1]["phase"] == w["phase"]:
                segs[-1]["weeks"] += 1
            else:
                segs.append({"phase": w["phase"], "weeks": 1})
        total = len(weeks)
        current = min(total, max(1, sum(1 for w in weeks if w["week_start"] <= this_wk)))
        for sg in segs:
            sg["pct"] = round(100 * sg["weeks"] / total, 2)
        return {"segments": segs, "total": total, "current": current, "pct": round(100 * (current - 0.5) / total, 1),
                "phase": next((w["phase"] for w in weeks if w["week_start"] == this_wk), segs[0]["phase"])}

    def full_plan(self) -> list[dict]:
        weeks = {w["week_start"]: {**w, "workouts": []} for w in self.db.get_weeks()}
        for w in self.db.get_workouts():
            weeks.setdefault(w["week_start"], {"week_start": w["week_start"], "phase": "", "target_km": None, "recovery": 0, "workouts": []})["workouts"].append(w)
        out = [weeks[k] for k in sorted(weeks)]
        for wk in out:
            wk["planned_km"] = round(sum(w["distance_km"] or 0 for w in wk["workouts"]), 1)
        return out

    def progress(self) -> dict:
        """Weekly planned vs. actual km across the plan, plus headline stats."""
        today = self.today()
        plan = self.full_plan()
        if not plan:
            return {"weeks": [], "stats": None}
        first = date.fromisoformat(plan[0]["week_start"])
        totals = adherence.weekly_km(self.db.activities_since(first))
        weeks = [
            {"week_start": wk["week_start"], "planned": wk["planned_km"], "target": wk["target_km"],
             "actual": round(totals.get(date.fromisoformat(wk["week_start"]), 0.0), 1),
             "current": wk["week_start"] == monday(today).isoformat(),
             "past": date.fromisoformat(wk["week_start"]) < monday(today)}
            for wk in plan
        ]
        ws = [w for w in self.db.get_workouts(end=today) if w["status"] in ("completed", "partial", "missed")]
        done = [w for w in ws if w["status"] != "missed"]
        stats = {
            "total_km": round(sum(w["actual"] for w in weeks), 1),
            "completed": len([w for w in ws if w["status"] == "completed"]), "scheduled": len(ws),
            "adherence_pct": round(100 * len(done) / len(ws)) if ws else None,
            "best_week_km": max((w["actual"] for w in weeks), default=0),
        }
        acts = self.db.activities_since(today - timedelta(days=365))
        long_pace = [a["moving_s"] / (a["distance_m"] / 1000) for a in acts if a["distance_m"] >= 3000 and a["moving_s"]]
        stats["longest_km"] = round(max((a["distance_m"] for a in acts), default=0) / 1000, 1)
        stats["best_pace_s_km"] = round(min(long_pace)) if long_pace else None
        stats["hours"] = round(sum(a["moving_s"] for a in acts) / 3600, 1)
        stats["runs_logged"] = len(acts)
        stats["streak"] = self.streak()
        return {"weeks": weeks, "stats": stats, "max": max([max(w["actual"], w["target"] or w["planned"]) for w in weeks] + [1])}


def _fmt_hms(s: int) -> str:
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
