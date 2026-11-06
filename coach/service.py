from __future__ import annotations

import threading
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from dataclasses import dataclass

from . import adherence
from . import knowledge as K
from .config import Settings
from .db import DB
from .actions import ActionsMixin
from .progress_views import ProgressMixin
from .errors import UserError  # noqa: F401  (re-exported for callers)
from .runview import build_run_view
from .strava import StravaError
from .models import DAYS, Profile
from .plancheck import mentions_pain, sanitize
from .skeleton import DEFAULT_EASY_PACE_S_KM, VolumeOutlook, Week, build_skeleton_info, monday

_LOCKS: dict[tuple, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
HISTORY_DAYS = 90
FIX_RETRY_RATIO = 0.25


@dataclass
class PlanInputs:
    weeks: list[Week]
    pace: float                       # easy pace (s/km) used for time budgets
    outlook: VolumeOutlook
    paces_raw: dict[str, tuple[float, float]] | None   # training paces in s/km (fast end, slow end), or None if unknown
    paces_basis: str
    run_days: int

    @property
    def paces(self) -> dict[str, str] | None:
        return K.pace_text(self.paces_raw) if self.paces_raw else None


class CoachService(ActionsMixin, ProgressMixin):
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
        skipped = {w["id"] for w in ws if w["status"] == "skipped"}
        for wid, m in adherence.match_workouts(ws, acts, today).items():
            if wid in skipped and m["status"] in ("planned", "missed"):
                continue                       # an athlete's deliberate skip stays a skip (unless they ran anyway)
            self.db.update_workout_status(wid, m["status"], m["actual_km"], m["activity_id"])

    # ---- plan generation / review ------------------------------------------
    def _require_profile(self) -> Profile:
        p = self.db.get_profile()
        if not p:
            raise UserError("Set up your goal and schedule first.")
        if p.race_date < self.today() + timedelta(days=3):
            raise UserError("The race date is too close (or past). Update your goal.")
        return p

    def _baseline(self, p: Profile, acts: list[dict], *, trust_actuals: bool) -> tuple[float, str]:
        """How much the athlete currently runs per week, and where that number came from. Never silently 0."""
        today = self.today()
        real = adherence.recent_baseline_km(acts, today)            # mean of last 4 complete weeks; None if no history at all
        if real is not None and (real >= 5 or trust_actuals):
            return real, "strava"
        last = max((date.fromisoformat(a["date"]) for a in acts), default=None)
        days_off = (today - last).days if last else 0
        if p.baseline_km > 0:
            if real is not None and days_off >= 10:                 # stated volume, but a real layoff since
                return p.baseline_km * K.restart_fraction(days_off), "restart"
            return p.baseline_km, "stated"
        if p.experience == "beginner":
            return 0.0, "stated"                                     # "just starting": the skeleton floors this at 8 km
        if real is not None:
            return max(K.assumed_baseline_km(p.experience) * 0.5, K.MIN_BASE_KM), "restart"
        return K.assumed_baseline_km(p.experience), "assumed"       # told to the athlete in the plan summary

    def _paces(self, p: Profile, acts: list[dict]) -> tuple[dict[str, tuple[float, float]] | None, str, float]:
        """Training paces (computed in code, not by the model), where they came from, and an easy pace for time budgets."""
        today = self.today()
        easy_est = adherence.easy_pace_s_km(acts, today)
        if p.goal_time_s:
            paces = K.training_paces(K.vdot(p.goal_km * 1000, p.goal_time_s))
            return paces, "goal time (Daniels VDOT)", sum(paces["easy"]) / 2
        if easy_est:
            return K.paces_from_easy(easy_est), "recent runs", easy_est
        return None, "none: prescribe by effort (RPE / talk test)", DEFAULT_EASY_PACE_S_KM

    def _skeleton(self, p: Profile, acts: list[dict], plan_start: date, *, trust_actuals: bool = False) -> "PlanInputs":
        today = self.today()
        base_km, source = self._baseline(p, acts, trust_actuals=trust_actuals)
        paces, basis, pace = self._paces(p, acts)
        run_days = sum(1 for a in p.availability.values() if a.available)
        weeks, outlook = build_skeleton_info(
            plan_start=plan_start, today=today, race_date=p.race_date, goal_km=p.goal_km, baseline_km=base_km,
            experience=p.experience, weekly_minutes=p.weekly_minutes, long_day_minutes=p.long_day_minutes,
            easy_pace_s_km=pace, run_days=run_days, baseline_source=source,
        )
        return PlanInputs(weeks, pace, outlook, paces, basis, run_days)

    def _context(self, p: Profile, inp: PlanInputs, acts: list[dict], plan_start: date, note: str = "") -> dict:
        today = self.today()
        totals = adherence.weekly_km(acts)
        weeks, pace, o = inp.weeks, inp.pace, inp.outlook
        done_runs = {w["date"] for w in self.db.get_workouts(today, today) if w["status"] in ("completed", "partial")}
        skipped_days = {w["date"] for w in self.db.get_workouts(today) if w["status"] == "skipped"}     # the athlete chose not to run then
        blocked = sorted(done_runs | skipped_days | ({today.isoformat()} if any(a["date"] == today.isoformat() for a in acts) else set()))
        last_run = max((date.fromisoformat(a["date"]) for a in acts), default=None)
        days_off = (today - last_run).days if last_run else None
        phases = {w.phase for w in weeks} | ({"taper"} if any(w.phase == "race" for w in weeks) else set())
        menu = K.workout_menu(p.goal_km, p.experience)
        ctx = {
            "today": today.isoformat(), "today_weekday": DAYS[today.weekday()],
            "earliest_workout_date": today.isoformat(), "blocked_dates": blocked,
            "athlete": {
                "name": p.name, "level": p.experience, "notes": p.notes,
                "goal": p.goal_label, "goal_km": p.goal_km, "race_date": p.race_date.isoformat(),
                "goal_time": _fmt_hms(p.goal_time_s) if p.goal_time_s else None,
                "weeks_until_race": (p.race_date - today).days // 7,
            },
            "availability": {d: p.availability[d].model_dump() for d in DAYS if d in p.availability},
            "long_run_day": p.long_run_day,
            "run_days_available": inp.run_days,
            "recommended_runs_per_week_for_this_goal_and_level": K.recommended_runs_per_week(p.goal_km, p.experience),
            "recent_training": {
                "weekly_km_last_6_complete_weeks": [round(totals.get(monday(today) - timedelta(weeks=i), 0.0), 1) for i in range(6, 0, -1)],
                "easy_pace_s_per_km_estimate": round(pace),
                "have_strava_history": bool(acts),
                "days_since_last_run": days_off,
            },
            "volume_plan": {
                **o.to_dict(),
                "meaning": "start_km/peak_km are the weekly distances the weekly skeleton below builds to; baseline_source says how "
                           "the starting point was determined. Do not under-deliver the weekly target_km.",
            },
            "training_paces_per_km": inp.paces,
            "training_paces_basis": inp.paces_basis,
            "workout_menu_by_phase": {ph: menu[ph] for ph in menu if ph in phases},
            "injury_or_pain_mentioned_in_check_in": mentions_pain(note),
            "skeleton": [
                {**{k: v for k, v in w.to_row().items() if k != "week_start"}, "week_start": w.week_start.isoformat(),
                 "max_quality_sessions": K.quality_limit(p.experience, w.phase, w.recovery, inp.run_days),
                 "already_done_km": round(totals.get(w.week_start, 0.0), 1) if w.week_start == monday(today) else 0.0}
                for w in weeks
            ],
        }
        if days_off is not None and days_off >= 10 and acts:
            ctx["layoff"] = {"days_since_last_run": days_off, "suggested_restart_share_of_previous_volume": K.restart_fraction(days_off)}
        return ctx

    def _run_coach(self, p: Profile, inp: PlanInputs, acts: list[dict], ctx: dict, mode: str, note: str = "") -> tuple[list[dict], str]:
        today = self.today()
        totals = adherence.weekly_km(acts)
        longest = max((a["distance_m"] / 1000 for a in acts if a["date"] >= (today - timedelta(days=28)).isoformat()), default=0.0)
        kwargs = dict(
            profile=p, weeks=inp.weeks, today=today, blocked_dates=set(ctx["blocked_dates"]),
            done_km_by_week={monday(today): totals.get(monday(today), 0.0)}, pace_s_km=inp.pace,
            level=p.experience, longest_recent_km=longest,
            suppress_quality_until=max([d for d in ((today + timedelta(days=7)) if mentions_pain(note) else None, self.pain_until()) if d] or [None],
                                       default=None),
        )
        out = self.coach.plan(ctx, mode=mode)
        workouts, notes = sanitize(out["workouts"], **kwargs)
        if notes and len(notes) > max(2, FIX_RETRY_RATIO * len(out["workouts"])):
            out = self.coach.plan(ctx, mode=mode, feedback=notes)
            workouts, notes = sanitize(out["workouts"], **kwargs)
        if not workouts:
            raise UserError("The coach returned no usable workouts. Check your availability and try again.")
        self.db.replace_plan_from(self.today(), [w.to_row() for w in inp.weeks if w.week_start >= monday(today)], workouts)
        self.refresh_statuses()
        text = inp.outlook.athlete_text(K.goal_name(p.goal_km), p.experience)
        self.db.kv_set("volume_outlook", {**inp.outlook.to_dict(), "text": text, "level": p.experience,
                                          "goal": K.goal_name(p.goal_km), "paces_s": inp.paces_raw, "paces_basis": inp.paces_basis})
        return workouts, out["summary"].rstrip() + "\n\n" + text

    def generate_plan(self) -> str:
        with self._lock:
            p = self._require_profile()
            today = self.today()
            acts = self.db.activities_since(today - timedelta(days=HISTORY_DAYS))
            plan_start = monday(today)
            self.db.kv_set("plan_start", plan_start.isoformat())
            inp = self._skeleton(p, acts, plan_start)
            ctx = self._context(p, inp, acts, plan_start)
            _, summary = self._run_coach(p, inp, acts, ctx, "generate")
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
            inp = self._skeleton(p, acts, plan_start, trust_actuals=True)   # a review follows what the athlete actually did
            ctx = self._context(p, inp, acts, plan_start, note)
            ctx["athlete_note"] = note
            ctx["last_4_weeks_review"] = self._review_block(acts)
            ctx["unplanned_runs_last_14_days"] = self._extra_runs(acts)
            ctx["fatigue_signal"] = self.fatigue_signal()          # ≥2 of the last 4 easy/long runs felt hard (RPE ≥7)
            ctx["athlete_changes_last_14_days"] = [
                {"date": w["date"], "title": w["title"], "change": "skipped" if w["status"] == "skipped" else f"moved from {w['moved_from']}",
                 **({"reason": w["note"]} if w["status"] == "skipped" and w["note"] else {})}
                for w in self.db.get_workouts(today - timedelta(days=14))     # no upper bound: a move to a distant date is still a decision
                if w["status"] == "skipped" or w["moved_from"]
            ]
            ctx["current_upcoming_plan"] = [
                {k: w[k] for k in ("date", "type", "title", "distance_km")} for w in self.db.get_workouts(today, today + timedelta(days=13))
                if w["status"] == "planned"
            ]
            _, summary = self._run_coach(p, inp, acts, ctx, "adjust", note)
            self.db.log("review", summary if not note else f"You said: {note}\n\n{summary}")
            self.db.kv_set("last_review_week", monday(today).isoformat())
            return summary

    def _review_block(self, acts: list[dict]) -> list[dict]:
        today = self.today()
        by_id = {a["id"]: a for a in acts}
        fb = self.db.feedback_map()
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
                    **({"felt": fb[w["activity_id"]]["feeling"], "rpe": fb[w["activity_id"]]["rpe"], "athlete_note": fb[w["activity_id"]]["note"][:200]}
                       if w["activity_id"] in fb else {}),
                    **({"athlete_skipped_because": w["note"]} if w["status"] == "skipped" else {}),
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

    # ---- run detail --------------------------------------------------------------------------------------
    def _activity_detail(self, activity_id: int) -> tuple[dict | None, str | None]:
        cached = self.db.get_activity_detail(activity_id)
        if cached:
            return cached, None
        if not getattr(self.strava, "connected", False):
            return None, "Connect Strava to see this run's route and kilometre splits."
        if not hasattr(self.strava, "fetch_activity"):
            return None, None
        try:
            detail = self.strava.fetch_activity(activity_id)
        except StravaError as e:
            return None, str(e)
        self.db.save_activity_detail(activity_id, detail)
        return detail, None

    def run_detail(self, activity_id: int) -> dict | None:
        act = self.db.get_activity(activity_id)            # scoped to this user: another user's id is simply "not found"
        if not act:
            return None
        workout = next((w for w in self.db.get_workouts() if w["activity_id"] == activity_id), None)
        detail, note = self._activity_detail(activity_id)
        paces = (self.db.kv_get("volume_outlook") or {}).get("paces_s")
        view = build_run_view(act, workout, detail, paces)
        view["feedback"] = self.db.get_feedback(activity_id)
        view["detail_note"] = note
        return view

    def recent_runs(self, limit: int = 12) -> list[dict]:
        fb = self.db.feedback_map()
        out = []
        for a in reversed(self.db.activities_since(self.today() - timedelta(days=90))):
            km = a["distance_m"] / 1000
            out.append({"id": a["id"], "date": a["date"], "name": a["name"] or "Run", "km": km,
                        "pace_s_km": a["moving_s"] / km if km else None, "feeling": (fb.get(a["id"]) or {}).get("feeling")})
            if len(out) >= limit:
                break
        return out

    # ---- views -----------------------------------------------------------
    def dashboard(self) -> dict:
        today = self.today()
        p = self.db.get_profile()
        data: dict = {"profile": p, "today": today, "strava_connected": self.strava.connected,
                      "strava_ready": getattr(self.strava, "configured", False),

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
        data["pending_feedback"] = self.pending_feedback()
        data["race"] = self.race_state()
        data["new_badges"] = self.badges()["new"]
        data["new_badge_text"] = " · ".join(f"{b['emoji']} {b['name']}" for b in data["new_badges"])
        tw = data["today_workout"]
        data["today_move_options"] = (self.move_options(tw["id"]) if tw and tw["status"] == "planned" and tw["type"] != "race" else [])
        data["pain_until"] = self.pain_until()
        data["journey"] = self.journey()
        return data

    def streak(self) -> int:
        """Consecutive most-recent scheduled runs that were done (fully or partly), stopping at the first miss."""
        past = [w for w in self.db.get_workouts(end=self.today()) if w["status"] in ("completed", "partial", "missed", "skipped")]
        n = 0
        for w in reversed(past):
            if w["status"] == "missed":
                break
            if w["status"] != "skipped":      # a deliberate skip neither extends nor breaks the streak
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
        return {"fitness": self.fitness(), "heatmap": self.heatmap(), "badges": self.badges(), "weeks": weeks, "stats": stats, "max": max([max(w["actual"], w["target"] or w["planned"]) for w in weeks] + [1])}


def _fmt_hms(s: int) -> str:
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
