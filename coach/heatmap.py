"""A GitHub-style grid of run days: one column per week (Mon at the top), shaded by distance."""
from __future__ import annotations

import statistics
from datetime import date, timedelta

from .skeleton import monday


def build(acts: list[dict], today: date, weeks: int = 20) -> dict:
    start = monday(today) - timedelta(weeks=weeks - 1)
    by_day: dict[str, dict] = {}
    for a in acts:
        d = a["date"]
        if date.fromisoformat(d) < start or date.fromisoformat(d) > today:
            continue
        cell = by_day.setdefault(d, {"km": 0.0, "run_id": a["id"], "runs": 0})
        cell["km"] += a["distance_m"] / 1000
        cell["runs"] += 1
        cell["run_id"] = a["id"]
    positives = sorted(c["km"] for c in by_day.values())
    thresholds = statistics.quantiles(positives, n=4) if len(positives) >= 4 else [3.0, 6.0, 10.0]
    cells = []
    for w in range(weeks):
        for dow in range(7):
            d = start + timedelta(weeks=w, days=dow)
            c = by_day.get(d.isoformat())
            km = round(c["km"], 1) if c else 0.0
            level = 0 if not c else 1 + sum(1 for t in thresholds if c["km"] > t)
            cells.append({"date": d.isoformat(), "km": km, "level": min(level, 4), "run_id": c["run_id"] if c else None, "future": d > today,
                          "col": w + 1, "row": dow + 1})
    months, last = [], None
    for w in range(weeks):
        m = (start + timedelta(weeks=w)).strftime("%b")
        if m != last:
            months.append({"col": w + 1, "label": m})
            last = m
    return {"cells": cells, "months": months, "weeks": weeks, "runs": sum(c["runs"] for c in by_day.values()), "active_days": len(by_day)}
