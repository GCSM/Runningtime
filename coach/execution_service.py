"""Execution scores for one athlete's runs, and the "make the plan slightly easier" suggestion."""
from __future__ import annotations

from datetime import date, timedelta

from . import execution as X


class ExecutionMixin:
    def _paces_s(self) -> dict | None:
        return (self.db.kv_get("volume_outlook") or {}).get("paces_s")

    def execution_for(self, w: dict) -> dict | None:
        """Score for a completed/partial workout (needs its matched run), else None."""
        if not w.get("activity_id"):
            return None
        p = self.db.get_profile()
        return X.score_run(w, self.db.get_activity(w["activity_id"]), self._paces_s(), p.units if p else "km")

    def execution_rows(self, days: int = X.WINDOW_DAYS) -> list[dict]:
        today = self.today()
        paces = self._paces_s()
        units = (self.db.get_profile().units if self.db.get_profile() else "km")
        rows = []
        for w in self.db.get_workouts(today - timedelta(days=days), today - timedelta(days=1)):
            if w["type"] == "race" or w["status"] not in ("completed", "partial", "missed"):
                continue                                              # skipped runs are the athlete's choice and aren't scored
            if w["status"] == "missed":
                rows.append({"date": w["date"], "score": 0, "missed": True, "pace_dev": None, "short": False})
                continue
            r = X.score_run(w, self.db.get_activity(w["activity_id"]) if w["activity_id"] else None, paces, units)
            if r:
                rows.append({"date": w["date"], "score": r["score"], "missed": False, "pace_dev": r["pace_dev"] if r["pace_score"] is not None else None,
                             "short": r["actual_km"] < 0.85 * w["distance_km"], "easy": w["type"] in X.EASY_TYPES})
        return rows

    def execution_overview(self, days: int = X.WINDOW_DAYS) -> dict | None:
        return X.overview(self.execution_rows(days))

    def ease_suggestion(self) -> dict | None:
        """Suggest an easier next two weeks when, after two weeks on the plan, the athlete is regularly under 70% execution."""
        p = self.db.get_profile()
        start = self.db.kv_get("plan_start")
        if not p or not start:
            return None
        today = self.today()
        if (today - date.fromisoformat(start)).days < X.WINDOW_DAYS:
            return None                                              # not enough time on the plan to call it a pattern
        if (p.race_date - today).days <= 21 or self.pain_until():    # no easing a taper; pain mode already eases things
            return None
        quiet = self.db.kv_get("ease_plan_quiet_until")
        if quiet and today.isoformat() < quiet:
            return None
        ov = self.execution_overview()
        if not ov or ov["n"] < X.MIN_RUNS or ov["avg"] >= X.THRESHOLD or ov["under"] * 2 < ov["n"]:
            return None
        if not [w for w in self.db.get_workouts(today, today + timedelta(days=13)) if w["status"] == "planned" and w["type"] != "race"]:
            return None
        why = X.REASON_TEXT.get(ov["main_reason"])
        return {**ov, "reason_text": why, "threshold": X.THRESHOLD}

    def dismiss_ease_suggestion(self, days: int = 7) -> None:
        self.db.kv_set("ease_plan_quiet_until", (self.today() + timedelta(days=days)).isoformat())
