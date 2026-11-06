"""Progress-side views for CoachService: fitness, heatmap, milestones, race week / post-race."""
from __future__ import annotations

from datetime import date, timedelta

from . import badges as B
from . import fitness as F
from . import heatmap as H
from . import knowledge as K
from .errors import UserError

ALL_TIME = date(2000, 1, 1)


class ProgressMixin:
    def _all_activities(self) -> list[dict]:
        return self.db.activities_since(ALL_TIME)

    def fitness(self) -> dict | None:
        p = self.db.get_profile()
        return F.view(self._all_activities(), self.today(), p.goal_km, p.goal_time_s) if p else None

    def heatmap(self, weeks: int = 20) -> dict:
        return H.build(self._all_activities(), self.today(), weeks)

    # ---- milestones ---------------------------------------------------------------------------------------
    def badges(self) -> dict:
        p = self.db.get_profile()
        ps = self.db.kv_get("plan_start")
        ctx = B.Ctx(acts=self._all_activities(), workouts=self.db.get_workouts(), today=self.today(),
                    goal_km=p.goal_km if p else 5.0, goal_time_s=p.goal_time_s if p else None,
                    plan_start=date.fromisoformat(ps) if ps else None, race_date=p.race_date if p else None)
        every = B.evaluate(ctx)
        earned = [b for b in every if b["earned"]]
        ids = {b["id"] for b in earned}
        seen = self.db.kv_get("badges_seen")
        if seen is None:                       # first look: what's already earned is history, not news (no confetti storm)
            self.db.kv_set("badges_seen", sorted(ids))
            new = []
        else:
            new = [b for b in earned if b["id"] not in set(seen)]
        return {"earned": sorted(earned, key=lambda b: b["earned"], reverse=True), "locked": [b for b in every if not b["earned"]],
                "new": new, "count": len(earned), "total": len(every)}

    def mark_badges_seen(self) -> None:
        self.db.kv_set("badges_seen", sorted(b["id"] for b in self.badges()["earned"]))

    # ---- race week, race day, after the race --------------------------------------------------------------
    def race_state(self) -> dict | None:
        p = self.db.get_profile()
        if not p:
            return None
        today = self.today()
        days = (p.race_date - today).days
        mode = "after" if days < 0 else "day" if days == 0 else "week" if days <= 7 else "normal"
        out: dict = {"mode": mode, "days": days}
        if mode in ("week", "day"):
            out["checklist"] = self.checklist()
            out["pace_plan"] = K.pace_plan(p.goal_km, p.goal_time_s, p.units) if p.goal_time_s else None
            out["pace_s_km"] = round(p.goal_time_s / p.goal_km) if p.goal_time_s else None
        if mode == "after":
            race = next((w for w in self.db.get_workouts() if w["type"] == "race"), None)
            act = self.db.get_activity(race["activity_id"]) if race and race["activity_id"] else None
            result = None
            if act:
                result = {"activity_id": act["id"], "km": act["distance_m"] / 1000, "time_s": act["moving_s"],
                          "pace_s_km": act["moving_s"] / (act["distance_m"] / 1000),
                          "hit_goal": (act["moving_s"] <= p.goal_time_s and act["distance_m"] >= p.goal_km * 970) if p.goal_time_s else None}
            out["result"], out["goal_label"] = result, p.goal_label
        return out

    def checklist(self) -> list[dict]:
        p = self.db.get_profile()
        saved = self.db.kv_get("race_checklist") or {}
        done = set(saved.get("done", [])) if saved.get("race_date") == p.race_date.isoformat() else set()   # a new race starts a fresh list
        return [{"id": i, "text": t, "done": i in done} for i, t in K.race_checklist(p.goal_km)]

    def toggle_checklist(self, item: str, checked: bool) -> None:
        p = self.db.get_profile()
        valid = {i for i, _ in K.race_checklist(p.goal_km)} if p else set()
        if item not in valid:
            raise UserError("That checklist item wasn't found.")
        done = {c["id"] for c in self.checklist() if c["done"]}
        (done.add if checked else done.discard)(item)
        self.db.kv_set("race_checklist", {"race_date": p.race_date.isoformat(), "done": sorted(done)})
