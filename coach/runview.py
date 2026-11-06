"""Everything the run page shows, computed from plain data (an activity row, the matched workout, cached Strava detail)."""
from __future__ import annotations

from . import polyline as P

ZONE_FOR_TYPE = {"easy": "easy", "recovery": "easy", "long": "easy", "tempo": "threshold", "intervals": "interval"}
ON_PLAN_TOLERANCE = 10.0       # ± percent of planned distance still counts as "right on plan"
BAND_TOLERANCE_S = 8           # seconds/km of slack when judging a split against the target pace


def build_run_view(act: dict, workout: dict | None, detail: dict | None, paces_s: dict | None) -> dict:
    km = act["distance_m"] / 1000
    moving = act["moving_s"]
    detail = detail or {}
    v = {
        "id": act["id"], "name": detail.get("name") or act.get("name") or "Run", "date": act["date"],
        "start_time": (act.get("start_local") or "")[11:16], "km": km, "moving_s": moving,
        "pace_s_km": moving / km if km else None, "avg_hr": act.get("avg_hr") or detail.get("average_heartrate"),
        "max_hr": detail.get("max_heartrate"), "elevation_m": act.get("elevation_m") if act.get("elevation_m") is not None else detail.get("total_elevation_gain"),
        "description": detail.get("description") or "", "workout": workout, "has_detail": bool(detail),
    }

    pts = P.decode(detail["polyline"]) if detail.get("polyline") else []
    v["route"] = P.to_svg(pts)

    band = None
    if workout and workout.get("type") in ZONE_FOR_TYPE and paces_s and paces_s.get(ZONE_FOR_TYPE[workout["type"]]):
        fast, slow = paces_s[ZONE_FOR_TYPE[workout["type"]]]
        band = (fast - BAND_TOLERANCE_S, slow + BAND_TOLERANCE_S)

    splits = []
    for sp in detail.get("splits") or []:
        dist, mt = sp.get("distance") or 0, sp.get("moving_time") or 0
        if dist < 200 or mt <= 0:
            continue
        splits.append({"n": sp.get("split"), "km": dist / 1000, "pace": mt / (dist / 1000), "hr": sp.get("average_heartrate"),
                       "elev": sp.get("elevation_difference"), "partial": dist < 950})
    full = [s for s in splits if not s["partial"]] or splits
    if splits:
        fast, slow = min(s["pace"] for s in full), max(s["pace"] for s in full)
        for s in splits:
            s["bar"] = 100 if slow <= fast else max(30, min(100, round(55 + 45 * (slow - s["pace"]) / (slow - fast))))
            uneven = len(full) > 1 and slow > fast            # perfectly even splits crown neither a fastest nor a slowest
            s["fastest"] = uneven and not s["partial"] and s["pace"] == fast
            s["slowest"] = uneven and not s["partial"] and s["pace"] == slow
            s["in_band"] = (band[0] <= s["pace"] <= band[1]) if band else None
    v["splits"] = splits
    v["band"] = band

    v["plan"] = None
    if workout:
        planned = workout.get("distance_km")
        diff = (km - planned) / planned * 100 if planned else None
        if diff is None:
            verdict = ""
        elif abs(diff) <= ON_PLAN_TOLERANCE:
            verdict = "Right on plan."
        elif diff > 0:
            verdict = f"{diff:.0f}% longer than planned."
        else:
            verdict = f"{abs(diff):.0f}% shorter than planned."
        notes = []
        if band and workout["type"] in ("easy", "recovery", "long") and v["pace_s_km"] and v["pace_s_km"] < band[0] - 5:
            notes.append("That's quicker than your easy range. Save the speed for the hard days; easy days work when they feel easy.")
        if band and splits:
            n_in = sum(1 for s in splits if s["in_band"])
            notes.append(f"{n_in} of {len(splits)} splits were in the target range.")
        v["plan"] = {"type": workout["type"], "title": workout["title"], "planned_km": planned, "planned_min": workout.get("duration_min"),
                     "pace_guidance": workout.get("pace_guidance") or "", "diff_pct": round(diff) if diff is not None else None,
                     "verdict": verdict, "notes": notes}
    return v
