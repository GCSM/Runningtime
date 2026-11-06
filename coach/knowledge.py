"""Coaching knowledge, as data and small pure functions.

Every constant here traces to a row in docs/RESEARCH.md (section numbers in comments). Nothing in this module talks to
the network or the language model, so it is cheap to test: tests/test_knowledge.py pins the values to the research.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

LEVELS = ("beginner", "intermediate", "advanced")
LEVEL_HELP = {  # McMillan-style definitions; shown on the setup form
    "beginner": "New to running, returning after a long break, or you haven't raced",
    "intermediate": "You run regularly and are comfortable with 30–60 minute runs; maybe a race or two",
    "advanced": "You train consistently most of the year and race to a time goal",
}
KM_PER_MI = 1.609344

# Goal anchors (km). Custom distances interpolate between them on a log scale.
ANCHORS = (5.0, 10.0, 21.0975, 42.195)


def _bucket(goal_km: float) -> float:
    """Nearest anchor *at or above* the goal's size class: ≤5.5→5K, ≤10.5→10K, ≤22→half, else marathon."""
    return 5.0 if goal_km <= 5.5 else 10.0 if goal_km <= 10.5 else 21.0975 if goal_km <= 22 else 42.195


def goal_name(goal_km: float) -> str:
    return {5.0: "5K", 10.0: "10K", 21.0975: "half marathon", 42.195: "marathon"}[_bucket(goal_km)]


# ---- 2. Weekly volume by goal and level: (low, target, high) peak km/week ------------------------------------------
# Ranges come from published plans (see RESEARCH.md §2). `target` sits in the lower-middle of the range because the
# athlete must be able to *reach* it from where they are; the ramp (below) and their schedule decide what is possible.
PEAK_KM: dict[str, dict[float, tuple[float, float, float]]] = {
    "beginner":     {5.0: (15, 22, 30), 10.0: (24, 30, 38), 21.0975: (32, 40, 52), 42.195: (48, 62, 90)},
    "intermediate": {5.0: (30, 38, 46), 10.0: (40, 48, 58), 21.0975: (45, 56, 70), 42.195: (60, 78, 95)},
    "advanced":     {5.0: (48, 60, 80), 10.0: (55, 65, 85), 21.0975: (65, 80, 105), 42.195: (80, 105, 140)},
}


def _interp(table: dict[float, tuple[float, float, float]], goal_km: float) -> tuple[float, float, float]:
    if goal_km <= ANCHORS[0]:
        return table[ANCHORS[0]]
    if goal_km >= ANCHORS[-1]:
        return table[ANCHORS[-1]]
    for a, b in zip(ANCHORS, ANCHORS[1:]):
        if a <= goal_km <= b:
            f = (math.log(goal_km) - math.log(a)) / (math.log(b) - math.log(a))
            return tuple(round(x + f * (y - x), 1) for x, y in zip(table[a], table[b]))  # type: ignore[return-value]
    return table[ANCHORS[-1]]


def peak_range(goal_km: float, level: str) -> tuple[float, float, float]:
    """(low, target, high) peak weekly km published plans use for this goal and level."""
    return _interp(PEAK_KM[_level(level)], goal_km)


def _level(level: str) -> str:
    return level if level in LEVELS else "intermediate"


# ---- When we don't know the athlete's current volume: a conservative, *reported* assumption (design choice) ---------
ASSUMED_BASELINE_KM = {"beginner": 12.0, "intermediate": 22.0, "advanced": 38.0}  # ≈ start-of-plan volumes of Higdon HM novice/intermediate
MIN_BASE_KM = 8.0


def assumed_baseline_km(level: str) -> float:
    return ASSUMED_BASELINE_KM[_level(level)]


# ---- 3. Progression and recovery weeks ----------------------------------------------------------------------------
RAMP_PCT = {"beginner": 0.08, "intermediate": 0.10, "advanced": 0.12}      # RunningFront / claude-coach
RAMP_FLOOR_KM = {"beginner": 1.5, "intermediate": 2.0, "advanced": 2.5}    # design choice: lets low volumes actually grow
RECOVERY_EVERY = 4                                                         # 3 build : 1 recovery
RECOVERY_FACTOR = 0.75                                                     # 25% cut (sources: 20–50%)
MIN_WEEKS_FOR_RECOVERY = 5


def next_week_km(level_km: float, level: str) -> float:
    lv = _level(level)
    return level_km + max(level_km * RAMP_PCT[lv], RAMP_FLOOR_KM[lv])


def max_weekly_increase_km(level_km: float, level: str) -> float:
    lv = _level(level)
    return max(level_km * RAMP_PCT[lv], RAMP_FLOOR_KM[lv])


# ---- 4. Long runs -------------------------------------------------------------------------------------------------
LONG_RUN_CAP_KM = {  # peak long run by event and level; upper end = claude-coach "Long Run Targets by Event"
    "beginner":     {5.0: 8, 10.0: 12, 21.0975: 16, 42.195: 28},
    "intermediate": {5.0: 11, 10.0: 15, 21.0975: 20, 42.195: 32},
    "advanced":     {5.0: 12, 10.0: 16, 21.0975: 22, 42.195: 35},
}
LONG_RUN_MAX_MIN = 150          # Daniels, as summarised: ≤150 min
LONG_RUN_MAX_MIN_MARATHON = 300  # 5 h: a time limit must not stop slower runners covering the distance (Higdon novice 20-miler ≈ 4 h+); the km caps (28–35) are the real limit
LONG_RUN_GROWTH_PCT = 0.10       # vs. longest run of the previous 4 weeks (Frandsen, secondary) …
LONG_RUN_GROWTH_FLOOR_KM = 2.5   # … or +2–3 km/week (claude-coach), whichever is larger


def long_run_share(week_km: float, run_days: int, goal_km: float = 10.0) -> float:
    """Share of weekly volume the long run may be.

    5K/10K: ~40% for 3-run weeks (Higdon Novice 5K ≈ 37%), else 30% (25% at ≥64 km/week) – the Daniels/coach rule.
    Half/marathon: higher (35% / 40%), because those plans routinely exceed 25–30% (claude-coach: marathon long runs of
    28–35 km at 60–90 km/week); the time cap (150/300 min) and event cap still bound the run.
    """
    b = _bucket(goal_km)
    if b >= 42:
        return 0.40
    if b >= 21:
        return 0.35
    if run_days <= 3:
        return 0.40
    return 0.25 if week_km >= 64 else 0.30


def long_run_cap_km(goal_km: float, level: str) -> float:
    return float(LONG_RUN_CAP_KM[_level(level)][_bucket(goal_km)])


def long_run_max_minutes(goal_km: float) -> int:
    return LONG_RUN_MAX_MIN_MARATHON if _bucket(goal_km) >= 42 else LONG_RUN_MAX_MIN


def long_run_km(week_km: float, goal_km: float, level: str, run_days: int, day_minutes: int, pace_s_km: float) -> float:
    limits = [long_run_share(week_km, run_days, goal_km) * week_km, long_run_cap_km(goal_km, level),
              long_run_max_minutes(goal_km) * 60 / pace_s_km]
    if day_minutes:
        limits.append(day_minutes * 60 / pace_s_km)
    return math.floor(min(limits) * 2) / 2   # round DOWN to 0.5 km so a cap is never exceeded


# ---- 6. Taper (fraction of peak volume per taper week; the last entry is race week and includes the race) ----------
TAPER = {5.0: [0.60], 10.0: [0.60], 21.0975: [0.75, 0.55], 42.195: [0.80, 0.65, 0.45]}


def taper_multipliers(goal_km: float) -> list[float]:
    return list(TAPER[_bucket(goal_km)])


# ---- 7. Plan length ----------------------------------------------------------------------------------------------
MIN_WEEKS = {  # (beginner, intermediate, advanced)
    5.0: (8, 6, 4), 10.0: (10, 8, 6), 21.0975: (12, 10, 8), 42.195: (20, 16, 16),
}


def min_weeks(goal_km: float, level: str) -> int:
    return MIN_WEEKS[_bucket(goal_km)][LEVELS.index(_level(level))]


# ---- 2/5. Runs per week (recommended) and hard-work rules ---------------------------------------------------------
def recommended_runs_per_week(goal_km: float, level: str) -> int:
    longer = _bucket(goal_km) >= 21
    return {"beginner": 4 if longer else 3, "intermediate": 5 if longer else 4, "advanced": 6 if longer else 5}[_level(level)]


def quality_limit(level: str, phase: str, recovery: bool, run_days: int) -> int:
    """Max tempo/interval sessions in a week (strides don't count). RESEARCH.md §5."""
    if recovery:
        return 0
    if phase in ("taper", "race"):
        return 1
    lv = _level(level)
    if phase == "base":
        return 0 if lv == "beginner" else 1
    return 1 if lv == "beginner" or (lv == "intermediate" and run_days < 4) else 2


QUALITY_TYPES = ("tempo", "intervals")
HARD_SHARE_MAX = 0.45        # hard-session *distance* ≤45% of the week: ~half of a session is warm-up/cool-down, so true hard work ≲22% (80/20: 85/15–75/25)
EASY_SHARE_MIN = 0.75        # ≥75% easy (Seiler: 85/15 – 75/25)


# ---- 9. Pace calculation ------------------------------------------------------------------------------------------
RIEGEL_EXPONENT = 1.06


def riegel(time_s: float, from_km: float, to_km: float, exponent: float = RIEGEL_EXPONENT) -> float:
    return time_s * (to_km / from_km) ** exponent


def _vo2_cost(v_m_min: float) -> float:
    return -4.60 + 0.182258 * v_m_min + 0.000104 * v_m_min ** 2


def _fraction(t_min: float) -> float:
    return 0.8 + 0.1894393 * math.exp(-0.012778 * t_min) + 0.2989558 * math.exp(-0.1932605 * t_min)


def vdot(distance_m: float, time_s: float) -> float:
    """Daniels/Gilbert VDOT from a race result."""
    t = time_s / 60
    return _vo2_cost(distance_m / t) / _fraction(t)


def _velocity_for_vo2(vo2: float) -> float:
    a, b, c = 0.000104, 0.182258, -(4.60 + vo2)
    return (-b + math.sqrt(b * b - 4 * a * c)) / (2 * a)


def race_time_s(v_dot: float, distance_m: float) -> float:
    """Time (s) at which a runner of this VDOT covers the distance (bisection; used for tests and goal checks)."""
    lo, hi = 60.0, 3600.0 * 12
    for _ in range(80):
        mid = (lo + hi) / 2
        if vdot(distance_m, mid) > v_dot:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# Intensity as a share of VO2max (RESEARCH.md §9; sources differ by a few points)
ZONE_INTENSITY = {"easy_slow": 0.59, "easy_fast": 0.74, "marathon": 0.80, "threshold": 0.88, "interval": 0.98, "repetition": 1.05}


def pace_s_per_km(v_dot: float, intensity: float) -> float:
    return 1000.0 / _velocity_for_vo2(v_dot * intensity) * 60


def training_paces(v_dot: float) -> dict[str, tuple[float, float]]:
    """Training paces in s/km as (fast_end, slow_end). Single-point zones return the same value twice."""
    z = ZONE_INTENSITY
    p = lambda i: pace_s_per_km(v_dot, i)  # noqa: E731
    return {
        "easy": (p(z["easy_fast"]), p(z["easy_slow"])),
        "marathon": (p(z["marathon"]),) * 2,
        "threshold": (p(z["threshold"]),) * 2,
        "interval": (p(z["interval"]),) * 2,
        "repetition": (p(z["repetition"]),) * 2,
    }  # type: ignore[return-value]


def paces_from_easy(easy_s_km: float) -> dict[str, tuple[float, float]]:
    """If only an easy pace is known (claude-coach zones.md offsets): T = easy − 60 s/km, M = T + 20, I = T − 17, R = T − 30."""
    t = easy_s_km - 60
    return {"easy": (easy_s_km - 15, easy_s_km + 15), "marathon": (t + 20,) * 2, "threshold": (t,) * 2,
            "interval": (t - 17,) * 2, "repetition": (t - 30,) * 2}  # type: ignore[return-value]


def fmt_pace(s: float) -> str:
    s = round(s)
    return f"{s // 60}:{s % 60:02d}/km"


def pace_text(paces: dict[str, tuple[float, float]]) -> dict[str, str]:
    out = {}
    for k, (fast, slow) in paces.items():
        out[k] = fmt_pace(fast) if abs(fast - slow) < 1 else f"{fmt_pace(fast)}–{fmt_pace(slow)}"
    return out


# ---- 11. Returning from a layoff ----------------------------------------------------------------------------------
def restart_fraction(days_off: int) -> float:
    """Share of previous weekly volume to restart at (RESEARCH.md §11; sources vary)."""
    if days_off < 5:
        return 1.0
    if days_off <= 10:
        return 0.70
    if days_off <= 30:
        return 0.60
    if days_off <= 90:
        return 0.50
    return 0.30


# ---- 10. Workout menus, by phase (standard session templates; RESEARCH.md §10) -------------------------------------
def workout_menu(goal_km: float, level: str) -> dict[str, list[str]]:
    b = _bucket(goal_km)
    easy = "Easy run: conversational (talk test), RPE 3–4"
    strides = "Strides: 4–8 × 15–30 s relaxed-fast with full recovery, after an easy run"
    long_ = "Long run: easy, conversational; builds endurance"
    base = [easy, strides, "Hill repeats: 6–10 × 30–45 s uphill hard, jog down", "Fartlek: 6–8 × 1 min on / 1 min off by feel", long_]
    if b <= 5.0:
        build = ["Tempo: 15–25 min continuous at threshold (about 25–30 s/mi slower than 5K pace) between easy warm-up and cool-down",
                 "Intervals: 6–8 × 400 m at 5K–3K pace, 400 m jog", "Intervals: 4–6 × 800 m at 5K pace, 2 min jog", easy, long_]
        peak = ["Intervals: 4–6 × 1000 m at 5K pace, 3 min jog", "Race simulation: 2 miles (3.2 km) continuous at goal 5K pace in the final 2–3 weeks",
                "Tempo: 15–20 min at threshold", easy, strides, long_]
    elif b <= 10.0:
        build = ["Tempo: 20–30 min continuous at threshold", "Cruise intervals: 3–4 × 1 mile (1.6 km) at threshold, 60–90 s jog",
                 "Intervals: 5–6 × 800 m–1000 m at 5K–10K pace", easy, long_]
        peak = ["Intervals: 4–5 × 1.5–2 km at 10K pace, 2–3 min jog", "Tempo: 25–35 min at threshold", "Progression long run: last 25% at tempo effort",
                easy, strides, long_]
    elif b <= 22:
        build = ["Tempo: 25–40 min continuous at threshold", "Cruise intervals: 4–5 × 1.6 km at threshold, 60–90 s jog",
                 "Progression long run: last 25% moving to half-marathon pace", easy, long_]
        peak = ["Half-marathon-pace segments: 2 × 5–6 km at goal pace within a long run or midweek", "Tempo: 30–40 min at threshold",
                "Intervals: 5 × 1 km at 10K pace for sharpness", easy, long_]
    else:
        build = ["Tempo: 30–40 min at threshold", "Medium-long run: 16–22 km easy midweek (intermediate/advanced)",
                 "Progression long run: final 25% at marathon effort", easy, long_]
        peak = ["Marathon-specific long run: mostly easy, final 8–16 km at goal marathon pace (not every week)", "Tempo: 30–40 min at threshold",
                "Medium-long run: 16–22 km", easy, long_]
    taper = ["Short race-pace efforts (e.g. 3–4 × 1 km at goal pace) to stay sharp, with much lower volume", easy, "Shakeout: 20–30 min easy + 4 strides, 1–2 days before the race"]
    if _level(level) == "beginner":
        base = ["Run/walk intervals if continuous running isn't comfortable yet (e.g. 1–2 min run / 1 min walk, building toward 30 min continuous)"] + base[:1] + [long_]
    return {"base": base, "build": build, "peak": peak, "taper": taper, "race": taper}


# ---- Is the plan we can build actually enough for the goal? ------------------------------------------------------
MIN_PEAK_LONG_KM = {5.0: 5.0, 10.0: 8.0, 21.0975: 13.0, 42.195: 25.0}   # shortest peak long run that still prepares for the event
MIN_VOLUME_SHARE = 0.85                                                   # peak weekly km below 85% of the published low end is flagged


def readiness_warnings(goal_km: float, level: str, *, peak_km: float, peak_long_km: float, limit: str, time_cap_km: float,
                       long_day_minutes: int, weekly_minutes: int, pace_s_km: float, weeks_to_race: int, min_weeks_: int,
                       ideal_low: float) -> list[str]:
    """Plain-language reasons the plan we can build would under-prepare the athlete, each with what to change."""
    out: list[str] = []
    goal = goal_name(goal_km)
    need_long = MIN_PEAK_LONG_KM[_bucket(goal_km)]
    if peak_long_km < need_long:
        mins = int(math.ceil(need_long * pace_s_km / 60 / 5) * 5)
        if long_day_minutes and long_day_minutes < mins:
            out.append(f"Your longest run would only reach {peak_long_km:.0f} km because your long-run day has {long_day_minutes} minutes. "
                       f"A {goal} needs a long run of at least {need_long:.0f} km, which takes about {mins} minutes at an easy pace. "
                       f"Add time to your long-run day (aim for {mins}+ minutes) in Goal & schedule.")
        else:
            out.append(f"Your longest run would only reach {peak_long_km:.0f} km; a {goal} plan needs at least {need_long:.0f} km. "
                       f"There isn't enough time or build-up before the race to get there safely.")
    if peak_km < ideal_low * MIN_VOLUME_SHARE:
        if limit == "time":
            mins = int(math.ceil(ideal_low * MIN_VOLUME_SHARE * pace_s_km / 60 / 10) * 10)
            out.append(f"Your running time ({weekly_minutes} min/week, about {time_cap_km:.0f} km) caps the build at {peak_km:.0f} km/week; "
                       f"published {level} {goal} plans peak around {ideal_low:.0f}+ km. You'd need roughly {mins} minutes a week "
                       f"(more days or longer runs) to train properly.")
        else:
            out.append(f"There are only {weeks_to_race} weeks to the race, so the build tops out at {peak_km:.0f} km/week "
                       f"(published {level} {goal} plans peak around {ideal_low:.0f}+). {min_weeks_}+ weeks is usual; consider a later race.")
    if out:
        out.append(f"This is the most your current schedule supports, and it won't fully prepare you for a {goal}. "
                   f"Fix the points above for a proper build.")
    return out


# ---- Volume outlook: what the athlete is told about their weekly kilometres ---------------------------------------
@dataclass
class VolumeOutlook:
    baseline_km: float
    baseline_source: str        # strava | stated | assumed | restart
    start_km: float
    peak_km: float
    ideal_low: float
    ideal_target: float
    ideal_high: float
    limit: str                  # goal | time | ramp | none
    time_cap_km: float
    weeks_to_race: int
    min_weeks: int
    peak_long_km: float = 0.0
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: (round(v, 1) if isinstance(v, float) else v) for k, v in self.__dict__.items()}

    def athlete_text(self, goal: str, level: str) -> str:
        src = {
            "strava": "your recent Strava runs",
            "stated": "the weekly distance you entered",
            "assumed": f"an assumed typical week for a {level} runner (you didn't tell me your current volume — update it in Goal & schedule if this is off)",
            "restart": "a cautious restart, because there's been very little recent running",
        }[self.baseline_source]
        text = (f"Volume: starting at about {self.start_km:.0f} km/week (based on {src}) and building to about {self.peak_km:.0f} km/week. "
                f"Published {level} {goal} plans typically peak around {self.ideal_low:.0f}–{self.ideal_high:.0f} km/week.")
        if self.limit == "time":
            text += (f" Your available running time (about {self.time_cap_km:.0f} km/week at an easy pace) is what limits the build; "
                     f"adding minutes on your running days would allow a stronger plan.")
        elif self.limit == "ramp":
            text += (f" There aren't enough weeks to reach that range safely from where you are now, so this is as far as a steady build can go "
                     f"in {self.weeks_to_race} weeks.")
        if self.weeks_to_race < self.min_weeks:
            text += f" Heads-up: {self.min_weeks}+ weeks is usual for this goal at your level, so this is a compressed build."
        return text


# ---- Race week (general guidance, deliberately conservative) ------------------------------------------------------
def race_checklist(goal_km: float) -> list[tuple[str, str]]:
    b = _bucket(goal_km)
    items = [
        ("kit", "Lay out your kit and shoes. Nothing new on race day."),
        ("register", "Bib or registration and start time confirmed."),
        ("travel", "Travel plan sorted, and aim to arrive 60–90 minutes early."),
        ("sleep", "Look after your sleep over the two nights before."),
        ("breakfast", "Breakfast you've eaten before long runs, 2–3 hours before the start."),
        ("warmup", "Plan a warm-up: 10–15 easy minutes plus a few strides."),
    ]
    if b >= 21:
        items += [("fuel", "Fuelling plan: only what you practised in training, and where you'll take it."),
                  ("pace", "Know your pace plan and your split times."),
                  ("chafe", "Anti-chafe and blister protection where you need it.")]
    if b >= 42:
        items.append(("shakeout", "A short, easy shakeout run the day before."))
    return items


def pace_plan(goal_km: float, goal_time_s: int, units: str = "km") -> list[dict]:
    """Even-pace checkpoints every 5 (km or miles), then the finish. Times are elapsed."""
    step_km = 5.0 * (KM_PER_MI if units == "mi" else 1.0)
    unit = "mi" if units == "mi" else "km"
    pace = goal_time_s / goal_km
    out, k = [], 1
    while step_km * k < goal_km - 0.4 * (KM_PER_MI if units == "mi" else 1.0):
        out.append({"label": f"{5 * k} {unit}", "elapsed_s": round(pace * step_km * k)})
        k += 1
    out.append({"label": "Finish", "elapsed_s": goal_time_s})
    return out
