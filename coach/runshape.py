"""Turn a planned workout into a timeline of music segments: how long each lasts and how intense the music should be.

Energy runs 1 (chill) to 5 (all-out). Pure code, no model: the shape of a run follows from its type and duration, so a
warm-up is always calmer than the hard part and the cool-down always winds back down.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

ENERGY_NAME = {1: "chill", 2: "easy groove", 3: "steady", 4: "driving", 5: "all-out"}
ENERGY_BPM = {1: (80, 115), 2: (110, 135), 3: (125, 150), 4: (140, 165), 5: (150, 180)}   # songs at half this tempo also suit the cadence


@dataclass
class Segment:
    name: str
    seconds: int
    energy: int

    @property
    def bpm(self) -> tuple[int, int]:
        return ENERGY_BPM[self.energy]

    def to_dict(self) -> dict:
        return {**asdict(self), "label": ENERGY_NAME[self.energy], "bpm_lo": self.bpm[0], "bpm_hi": self.bpm[1]}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def total_seconds(workout: dict, easy_pace_s_km: float = 390.0) -> int:
    """How long the run will take: the planned duration, else distance at an easy pace. Never below 10 minutes."""
    mins = workout.get("duration_min") or 0
    if not mins and workout.get("distance_km"):
        mins = workout["distance_km"] * easy_pace_s_km / 60
    return int(max(10, mins) * 60)


def _reps(text: str) -> int | None:
    m = re.search(r"(\d{1,2})\s*[x×]\s*\d", text or "", re.I)
    n = int(m.group(1)) if m else None
    return n if n and 2 <= n <= 30 else None


def segments_for(workout: dict, easy_pace_s_km: float = 390.0) -> list[Segment]:
    """Segments that add up to exactly the run's duration."""
    total = total_seconds(workout, easy_pace_s_km)
    kind = workout.get("type", "easy")
    text = f"{workout.get('title', '')} {workout.get('description', '')}"
    mins = total / 60

    def build(parts: list[tuple[str, float, int]]) -> list[Segment]:
        """parts are (name, share-of-run, energy); shares are scaled to the total and the last segment absorbs rounding."""
        s = sum(p[1] for p in parts)
        segs, used = [], 0
        for i, (name, share, energy) in enumerate(parts):
            sec = total - used if i == len(parts) - 1 else int(round(total * share / s / 5) * 5)
            segs.append(Segment(name, sec, energy))
            used += sec
        return segs

    if kind == "race":
        warm = min(_clamp(mins * 0.18, 8, 15) / mins, 0.4)
        return build([("Pre-race warm-up", warm, 3), ("Race pace", 1 - warm - 0.06, 4), ("Final push", 0.06, 5)]) if mins >= 40 \
            else build([("Warm-up", 0.3, 3), ("Race", 0.55, 5), ("Finish", 0.15, 5)])
    if kind in ("tempo", "intervals"):
        warm = _clamp(mins * 0.22, 8, 15) / mins
        cool = _clamp(mins * 0.18, 6, 12) / mins
        if warm + cool > 0.7:                                  # a very short session: keep at least 30% for the work itself
            f = 0.7 / (warm + cool)
            warm, cool = warm * f, cool * f
        main = 1 - warm - cool
        if kind == "tempo":
            return build([("Warm-up", warm, 2), ("Tempo", main, 4), ("Cool-down", cool, 1)])
        reps = _reps(text)
        label = f"Intervals ({reps} reps)" if reps else "Intervals"
        return build([("Warm-up", warm, 2), ("Build", main * 0.25, 3), (label, main * 0.75, 5), ("Cool-down", cool, 1)])
    if kind == "long":
        if mins < 60:
            return build([("Warm-up", 0.15, 2), ("Cruise", 0.7, 3), ("Cool-down", 0.15, 2)])
        return build([("Warm-up", 8 / mins, 2), ("Settle in", 0.4, 3), ("Keep going", 0.38, 3), ("Last push", 0.1, 4), ("Cool-down", 6 / mins, 1)])
    if kind == "recovery":
        return build([("Recovery", 1, 1)])
    if kind == "cross":
        return build([("Warm-up", 0.2, 2), ("Main set", 0.65, 3), ("Cool-down", 0.15, 1)])
    # easy
    if mins < 25:
        return build([("Easy jog", 1, 2)])
    return build([("Warm-up", 0.15, 1), ("Easy cruise", 0.7, 2), ("Cool-down", 0.15, 1)])
