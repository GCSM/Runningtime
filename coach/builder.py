"""Code-built day-level training plans: a session library with progression ladders, and a scheduler that places the sessions.

Published plans (Higdon, Pfitzinger, Hansons, Daniels, FIRST, Galloway) are rule- and table-driven: a fixed weekly pattern, sessions that
step up along a ladder, a long run that grows, a taper. This module does the same in code, from the weekly skeleton (`skeleton.py`) that
already fixes each week's volume, long run, phase and step-backs. Every number in the plan is therefore reproducible and testable; the
language model is only needed for words (summary, notes) and for turning free-text requests into changes that code then validates.

`BuilderCoach.plan(ctx, mode=...)` has the same shape as `llm.Coach.plan`, so the service can use either one, and the same sanitizer
and audit run over its output.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from . import knowledge as K
from .models import DAYS

# ------------------------------------------------------------------------------------------------------------------------
# Session ladders. Each rung is a little harder than the one before; the athlete climbs from the first rung (start of the build) to the
# last (peak). Distances in metres, times in seconds.
# ------------------------------------------------------------------------------------------------------------------------
# Threshold work as minutes at threshold pace: (low, high) by goal bucket, scaled by level.
TEMPO_MINUTES = {5.0: (12, 22), 10.0: (15, 30), 21.0975: (20, 40), 42.195: (20, 45)}
LEVEL_TEMPO_SCALE = {"beginner": 0.7, "intermediate": 1.0, "advanced": 1.1}
TEMPO_MIN_MINUTES = 10

# Interval ladders: (reps, rep distance m, recovery s). Paced at the interval (≈5K) pace, or at goal pace for race-specific rungs.
INTERVALS = {
    5.0: [(6, 400, 90), (8, 400, 90), (5, 600, 120), (4, 800, 120), (5, 800, 120), (6, 800, 120), (4, 1000, 150), (5, 1000, 150)],
    10.0: [(6, 400, 90), (5, 600, 120), (5, 800, 120), (6, 800, 120), (5, 1000, 150), (6, 1000, 150), (5, 1200, 150), (4, 1600, 180)],
    21.0975: [(6, 600, 120), (5, 800, 120), (6, 800, 120), (5, 1000, 150), (6, 1000, 150), (5, 1200, 150), (4, 1600, 180)],
    42.195: [(6, 600, 120), (5, 800, 120), (6, 800, 120), (5, 1000, 150), (6, 1000, 150), (5, 1200, 150)],
}
# Race-specific interval ladders (at goal pace): used in the peak phase of 5K and 10K plans.
RACE_INTERVALS = {
    5.0: [(5, 600, 120), (5, 800, 120), (4, 1000, 150), (5, 1000, 150)],
    10.0: [(4, 1000, 120), (5, 1000, 120), (4, 1500, 150), (3, 2000, 180)],
}
# Fartlek ladder (base phase and beginners): (reps, on seconds, off seconds), done by feel at about 5K effort.
FARTLEK = [(6, 60, 90), (8, 60, 90), (6, 90, 90), (8, 90, 90), (6, 120, 120), (8, 120, 120)]
# Run/walk ladder for beginners: (run seconds, walk seconds); one rung per week until running is continuous.
RUN_WALK = [(60, 60), (90, 60), (120, 60), (180, 60), (300, 60), (480, 60), (600, 60), (900, 60)]
RUN_WALK_START_KM = [(6.0, 0), (10.0, 2), (15.0, 4)]       # baseline weekly km below this → start on that rung (above 15 km: continuous)
WALK_PACE_FACTOR = 1.12                                    # run/walk sessions cover ground ~12% slower than the same time spent running easy

# Paced portions of long runs: kilometres of marathon / half-marathon pace at occurrence n (0, 1, 2, …).
MP_BLOCK_KM = [5, 6, 8, 10, 12, 13, 15]                    # Pfitzinger's marathon-pace long runs reach 10-12 miles inside a 18-20 mile run
HMP_BLOCK_KM = [3, 4, 6, 8, 10]
PACED_LONG_MAX_SHARE = {42.195: 0.50, 21.0975: 0.40}       # share of the long run at pace
PACED_LONG_FROM = 0.5                                      # paced long runs start in the second half of the training weeks, every other week
MP_RUN_MINUTES = {42.195: (25, 70), 21.0975: (20, 45)}     # steady runs at marathon / half-marathon pace (Pfitzinger: up to 10-13 miles)
HILLS = [(6, 45), (8, 45), (8, 60), (10, 60), (8, 75)]     # hill repeats: (reps, seconds uphill), jog back down
PYRAMIDS = {5.0: [(400, 800, 1200, 800, 400)], 10.0: [(400, 800, 1200, 800, 400), (800, 1200, 1600, 1200, 800)],
            21.0975: [(800, 1200, 1600, 1200, 800)], 42.195: [(800, 1200, 1600, 1200, 800)]}
BENCHMARK_SHARE = (0.35, 0.70)                             # benchmark time trials at these fractions of the training weeks

THRESHOLD_SHARE = {42.195: 0.12}                           # share of a week's km that threshold work may take (10% otherwise, marathon 12%)
THRESHOLD_SHARE_DEFAULT = 0.10
INTERVAL_SHARE = 0.08
WARMUP_MIN = {"beginner": 8, "intermediate": 10, "advanced": 12}
QUALITY_BUDGET = 0.40                                      # all hard sessions together ≤ this share of the week's km (guard allows 45%)
MIN_EASY_KM = {"beginner": 2.0, "intermediate": 3.0, "advanced": 3.0}
EASY_MAX_OF_LONG = 0.85                                    # an easy run never exceeds this share of the long run
STRIDES = "6 × 20 s relaxed-fast strides with full walking recovery"
WEEKDAY_PREFERENCE = {1: 5, 3: 5, 2: 4, 4: 3, 0: 2, 5: 2, 6: 1}   # Tue/Thu are the classic quality days


def _bucket(goal_km: float) -> float:
    return K._bucket(goal_km)


# ------------------------------------------------------------------------------------------------------------------------
@dataclass
class Session:
    type: str
    title: str
    description: str
    km: float
    minutes: float
    pace_guidance: str
    hard_km: float = 0.0           # the kilometres at threshold/interval/race pace (what the share caps limit)


@dataclass
class Athlete:
    level: str
    goal_km: float
    bucket: float
    race_date: date
    goal_time_s: float | None
    units: str
    easy: float                    # s/km used for time budgets
    paces: dict[str, float]        # numeric s/km: easy, marathon, threshold, interval, repetition
    text: dict[str, str] | None    # paces as text in the athlete's units
    race_text: dict[str, str] | None
    goal_pace: float | None
    long_day: int
    avail: dict[int, tuple[bool, int]]
    run_days: int
    baseline_km: float
    pain: bool
    peak_long_km: float
    wu_override: int | None = None   # longer warm-up/cool-down when a week's km can't be carried by its easy days


def _mid(v) -> float:
    return (v[0] + v[1]) / 2 if isinstance(v, (list, tuple)) else float(v)


def parse(ctx: dict) -> Athlete:
    a = ctx["athlete"]
    easy = float(ctx["recent_training"]["easy_pace_s_per_km_estimate"])
    raw = ctx.get("training_paces_s_per_km")
    paces = {k: _mid(v) for k, v in raw.items()} if raw else {k: _mid(v) for k, v in K.paces_from_easy(easy).items()}
    units = ctx.get("units", "km")
    text = ctx.get("training_paces_per_mile" if units == "mi" else "training_paces_per_km")
    race_text = ctx.get("race_paces_per_mile" if units == "mi" else "race_paces_per_km")
    goal_time = None
    if a.get("goal_time"):
        parts = [int(x) for x in str(a["goal_time"]).split(":")]
        goal_time = float(parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) > 2 else 0)) if len(parts) >= 2 else None
    goal_km = float(a["goal_km"])
    sk = ctx["skeleton"]
    return Athlete(
        level=a["level"], goal_km=goal_km, bucket=_bucket(goal_km), race_date=date.fromisoformat(a["race_date"]), goal_time_s=goal_time, units=units,
        easy=easy, paces=paces, text=text, race_text=race_text, goal_pace=(goal_time / goal_km) if goal_time else None,
        long_day=DAYS.index(ctx["long_run_day"]),
        avail={i: (bool(ctx["availability"][d]["available"]), int(ctx["availability"][d].get("minutes") or 0)) for i, d in enumerate(DAYS) if d in ctx["availability"]},
        run_days=int(ctx.get("run_days_available") or sum(1 for v in ctx["availability"].values() if v["available"])),
        baseline_km=float(ctx.get("volume_plan", {}).get("baseline_km") or 0.0), pain=bool(ctx.get("injury_or_pain_mentioned_in_check_in")),
        peak_long_km=max((w["long_run_km"] for w in sk if w["phase"] in ("base", "build", "peak")), default=0.0),
    )


# ------------------------------------------------------------------------------------------------------------------------
# Text helpers (units-aware)
def dist_label(metres: int, units: str) -> str:
    if units == "mi" and metres % 1609 < 40 or metres in (1600, 3200) and units == "mi":
        n = round(metres / 1609.34)
        return f"{n} mile" + ("s" if n != 1 else "")
    if metres >= 1000 and metres % 1000 == 0 and units == "km":
        return f"{metres // 1000} km"
    if metres >= 1500 and units == "km":
        return f"{metres / 1000:g} km"
    return f"{metres} m"


def mins_text(seconds: float) -> str:
    s = round(seconds)
    return f"{s // 60}:{s % 60:02d}" if s % 60 else f"{s // 60} min"


def pace_cue(a: Athlete, key: str, fallback: str) -> str:
    """The pace for a zone as text in the athlete's units, or an effort cue when no pace is known."""
    if a.text and a.text.get(key):
        return f"{a.text[key]} ({fallback})"
    return fallback


def goal_pace_text(a: Athlete) -> str:
    if a.race_text and a.race_text.get("goal"):
        return f"goal pace {a.race_text['goal']}"
    return "goal race effort"


def _km(seconds: float, pace: float) -> float:
    return seconds / pace


def warmup_cooldown(a: Athlete) -> tuple[float, float]:
    """(minutes, km) of the warm-up (the cool-down is the same)."""
    m = a.wu_override or WARMUP_MIN.get(a.level, 10)
    return m, m * 60 / a.easy


# ------------------------------------------------------------------------------------------------------------------------
# Session constructors
def tempo_session(a: Athlete, minutes: int, cruise: bool | int, *, pace_key: str = "threshold", label: str = "threshold", title_word: str = "Tempo") -> Session:
    wu_m, wu_km = warmup_cooldown(a)
    p = a.paces[pace_key]
    mp = pace_key == "marathon"
    hm = label == "half-marathon" and bool(a.goal_pace) and a.bucket == 21.0975       # a half-marathon-pace run is run at the goal pace, not threshold
    goal_paced = (mp and a.goal_pace and a.bucket >= 42) or hm
    if goal_paced:
        p = a.goal_pace
    effort = "marathon effort, steady and controlled, RPE 5–6" if mp else (f"half-marathon effort, strong but controlled, RPE 6–7" if hm else f"{label} pace, comfortably hard, RPE 7")
    cue = goal_pace_text(a) + f" ({effort})" if goal_paced else pace_cue(a, pace_key, effort)
    if cruise:
        n = cruise if isinstance(cruise, int) and not isinstance(cruise, bool) and cruise >= 2 else max(2, round(minutes / (6 if minutes <= 24 else 8)))
        each = max(5, round(minutes / n))
        work_s = n * each * 60
        rec_s = (n - 1) * 60
        km = 2 * wu_km + work_s / p + rec_s / (a.easy * 1.2)
        mins = 2 * wu_m + n * each + (n - 1)
        title = f"Cruise intervals {n} × {each} min"
        desc = (f"Warm up {wu_m} min easy. Then {n} × {each} min at {cue}, "
                f"1 min easy jog between. Cool down {wu_m} min easy.")
    else:
        work_s = minutes * 60
        km = 2 * wu_km + work_s / p
        mins = 2 * wu_m + minutes
        title = f"{title_word} {minutes} min"
        desc = (f"Warm up {wu_m} min easy. Then {minutes} min continuous at {cue}. "
                f"Cool down {wu_m} min easy.")
    return Session("tempo", title, desc, km, mins, cue, hard_km=work_s / p)


def interval_session(a: Athlete, rung: tuple[int, int, int], *, at_goal: bool = False) -> Session:
    reps, metres, rec_s = rung
    wu_m, wu_km = warmup_cooldown(a)
    if at_goal and a.goal_pace:
        p, cue = a.goal_pace, goal_pace_text(a)
    elif at_goal:
        p, cue = a.paces["interval"] * 1.03, "goal race effort"
    else:
        p, cue = a.paces["interval"], pace_cue(a, "interval", "5K effort, hard but controlled, RPE 8")
    work_km = reps * metres / 1000
    rec_km = (reps - 1) * rec_s / (a.easy * 1.25)
    km = 2 * wu_km + work_km + rec_km
    mins = 2 * wu_m + work_km * p / 60 + (reps - 1) * rec_s / 60
    lab = dist_label(metres, a.units)
    desc = (f"Warm up {wu_m} min easy with a few strides. Then {reps} × {lab} at {cue}, {mins_text(rec_s)} easy jog between. "
            f"Cool down {wu_m} min easy.")
    return Session("intervals", f"{'Race-pace intervals' if at_goal else 'Intervals'} {reps} × {lab}", desc, km, mins,
                   cue if at_goal else pace_cue(a, "interval", "5K effort"), hard_km=work_km)


def fartlek_session(a: Athlete, rung: tuple[int, int, int]) -> Session:
    reps, on_s, off_s = rung
    wu_m, wu_km = warmup_cooldown(a)
    p = a.paces["interval"] * 1.08
    km = 2 * wu_km + reps * on_s / p + reps * off_s / (a.easy * 1.1)
    mins = 2 * wu_m + reps * (on_s + off_s) / 60
    desc = (f"Warm up {wu_m} min easy. Then {reps} × ({mins_text(on_s)} faster by feel at about 5K effort, RPE 7–8, then {mins_text(off_s)} easy jog). "
            f"No watch-watching: the effort matters, not the pace. Cool down {wu_m} min easy.")
    return Session("intervals", f"Fartlek {reps} × {mins_text(on_s)}", desc, km, mins, "by feel: hard but relaxed, RPE 7–8", hard_km=reps * on_s / p)


def hill_session(a: Athlete, rung: tuple[int, int]) -> Session:
    reps, on_s = rung
    wu_m, wu_km = warmup_cooldown(a)
    km = 2 * wu_km + reps * (on_s / (a.paces["interval"] * 1.25) + on_s * 1.5 / (a.easy * 1.1))
    mins = 2 * wu_m + reps * (on_s * 2.5) / 60
    desc = (f"Warm up {wu_m} min easy to a hill of about 4–6% grade. Then {reps} × {on_s} s uphill, hard but smooth (RPE 8), jogging easily back down "
            f"for recovery. Strong knees and arms, short quick steps. Cool down {wu_m} min easy.")
    return Session("intervals", f"Hill repeats {reps} × {on_s} s", desc, km, mins, "hard uphill effort (RPE 8), easy jog down", hard_km=reps * on_s / (a.paces["interval"] * 1.25))


def pyramid_session(a: Athlete, steps: tuple[int, ...]) -> Session:
    wu_m, wu_km = warmup_cooldown(a)
    p = a.paces["interval"]
    work_km = sum(steps) / 1000
    rec_s = 90 if max(steps) <= 800 else 120
    km = 2 * wu_km + work_km + (len(steps) - 1) * rec_s / (a.easy * 1.25)
    mins = 2 * wu_m + work_km * p / 60 + (len(steps) - 1) * rec_s / 60
    labs = " – ".join(str(m) for m in steps)
    desc = (f"Warm up {wu_m} min easy. Then a ladder: {', '.join(dist_label(m, a.units) for m in steps)} at {pace_cue(a, 'interval', '5K effort, hard but controlled, RPE 8')}, "
            f"{mins_text(rec_s)} easy jog between each. Cool down {wu_m} min easy.")
    return Session("intervals", f"Pyramid {labs} m", desc, km, mins, pace_cue(a, "interval", "5K effort"), hard_km=work_km)


def benchmark_session(a: Athlete) -> Session:
    """A time trial that gives the app a clean, current fitness number: paces are recalibrated from it."""
    km_tt = 3.0 if a.paces["interval"] * 3 / 60 >= 12.5 else 5.0       # long enough (12+ min) for a reliable fitness estimate
    wu_m, wu_km = warmup_cooldown(a)
    p = a.paces["interval"] * 1.02
    km = 2 * wu_km + km_tt
    mins = 2 * wu_m + km_tt * p / 60
    lab = K.fmt_dist(km_tt, a.units, 0 if abs(km_tt - round(km_tt)) < 0.05 and a.units == "km" else 1)
    desc = (f"Warm up {wu_m} min easy with 4 strides. Then run {lab} as fast as you can hold evenly: start controlled, finish strong. "
            f"Record the time trial as its own activity or lap so the app can read your time. Cool down {wu_m} min easy. "
            f"Your paces are recalibrated from this result.")
    return Session("intervals", f"Benchmark: {lab} time trial", desc, km, mins, "evenly paced all-out effort (RPE 9)", hard_km=km_tt)


def sharpener_session(a: Athlete, taper_step: int) -> Session:
    """A short goal-pace session in the taper that keeps the legs sharp (the volume falls, the intensity stays)."""
    if a.bucket >= 21:
        metres, reps = 1600, max(2, 3 - taper_step)
        s = tempo_like_goal(a, reps, metres)
        return s
    reps = max(3, 5 - taper_step)
    return interval_session(a, (reps, 800 if a.bucket >= 10 else 600, 120), at_goal=True)


def tempo_like_goal(a: Athlete, reps: int, metres: int) -> Session:
    wu_m, wu_km = warmup_cooldown(a)
    p = a.goal_pace or a.paces["marathon"]
    work_km = reps * metres / 1000
    km = 2 * wu_km + work_km + (reps - 1) * 90 / (a.easy * 1.25)
    mins = 2 * wu_m + work_km * p / 60 + (reps - 1) * 1.5
    lab = dist_label(metres, a.units)
    cue = goal_pace_text(a) if a.goal_pace else pace_cue(a, "marathon", "goal race effort")
    desc = f"Warm up {wu_m} min easy. Then {reps} × {lab} at {cue}, 90 s easy jog between. Cool down {wu_m} min easy. Short and sharp: you should finish feeling fresh."
    return Session("tempo", f"Goal-pace sharpener {reps} × {lab}", desc, km, mins, cue, hard_km=work_km)


def easy_session(a: Athlete, km: float, *, strides: bool = False, title: str | None = None, run_walk: tuple[int, int] | None = None,
                 kind: str = "easy", transition: bool = False) -> Session:
    cue = pace_cue(a, "easy", "conversational, RPE 3–4")
    psent = f" Pace {cue}." if (a.text and a.text.get("easy")) else ""      # without a known pace the RPE cue is already in the sentence
    if run_walk:
        mins = km * a.easy * WALK_PACE_FACTOR / 60
        run_s, walk_s = run_walk_for(run_walk, mins)
        reps = max(1, round(mins * 60 / (run_s + walk_s)))
        mins = reps * (run_s + walk_s) / 60 if reps else mins
        km = round(mins * 60 / (a.easy * WALK_PACE_FACTOR), 1)
        rw = f"{mins_text(run_s)} run / {mins_text(walk_s)} walk"
        t = title or f"Run/walk {reps} × ({rw})"
        mins += 5
        d = f"Start with a 5 min brisk walk. Then {reps} × ({rw}): run slowly enough to chat, and walk briskly in the walk breaks."
        return Session(kind, t, d, km, mins, "run slowly, chat easily (RPE 3–4); walk breaks are part of the plan")
    mins = km * a.easy / 60
    base = {"long": "Long run", "medium": "Medium-long run"}.get(kind, "Easy run")
    t = title or (base + (" + strides" if strides else ""))
    aid = " Practise taking a drink or gel on the move." if a.bucket >= 21 else ""
    if kind == "long":
        d = f"Relaxed and conversational the whole way (RPE 3–4): the aim is time on your feet, not speed.{aid}{psent}"
    elif kind == "medium":
        d = f"Steady and conversational, a little longer than your other weekday runs (RPE 3–4).{psent}"
    else:
        d = f"Easy and conversational (RPE 3–4): you should be able to talk in full sentences.{psent}"
    if transition:
        d += " You are moving from run/walk to continuous running: run the whole way, but take a 1 min walk break if you need one."
    if strides:
        d += f" After the run: {STRIDES}."
    return Session("long" if kind == "long" else "easy", t, d, km, mins, cue)


def _d1(km: float, units: str) -> str:
    """Distance for text with one decimal only when needed, so '25.5 km + 5 km' adds up to the 30.5 km long run."""
    v = km / (K.KM_PER_MI if units == 'mi' else 1.0)
    return f"{v:.1f}".rstrip('0').rstrip('.') + (' mi' if units == 'mi' else ' km')


def paced_long_session(a: Athlete, km: float, block_km: float) -> Session:
    key = "marathon" if a.bucket >= 42 else "marathon"      # the paced part of a long run is marathon (or half-marathon) pace
    name = "marathon pace" if a.bucket >= 42 else "half-marathon pace"
    p = a.goal_pace if (a.goal_pace and a.bucket in (21.0975, 42.195)) else (a.paces["marathon"] if a.bucket >= 42 else a.paces["threshold"] * 1.04)
    cue = goal_pace_text(a) if (a.goal_pace and a.bucket in (21.0975, 42.195)) else pace_cue(a, key, name)
    easy_km = km - block_km
    mins = easy_km * a.easy / 60 + block_km * p / 60
    t = f"Long run with {round(block_km)} km at {name}" if a.units == "km" else f"Long run with {block_km / K.KM_PER_MI:.0f} mi at {name}"
    d = (f"First {_d1(easy_km, a.units)} easy and conversational. Then {_d1(block_km, a.units)} at {cue}: controlled, even splits, "
         f"practice taking fuel. Last few minutes easy.")
    return Session("long", t, d, km, mins, cue, hard_km=block_km)


# ------------------------------------------------------------------------------------------------------------------------
# Fitting a session into its week: the highest rung that respects the day's minutes and the hard-work share caps.
def fits(a: Athlete, s: Session, week_km: float, day_minutes: int, budget_km: float, kind: str) -> bool:
    cap_min = K.run_cap_minutes(a.level, "tempo", a.goal_km)
    if day_minutes:
        cap_min = min(cap_min, day_minutes)
    if s.minutes > cap_min - 1 or s.km > budget_km + 0.05:
        return False
    if kind == "bench":
        return True
    share = THRESHOLD_SHARE.get(a.bucket, THRESHOLD_SHARE_DEFAULT) if kind == "tempo" else INTERVAL_SHARE
    return s.hard_km <= share * week_km + 0.05


def climb(ladder_len: int, p: float) -> int:
    """Rung for a progress p in [0, 1] through the quality weeks of the plan."""
    return max(0, min(ladder_len - 1, round(p * (ladder_len - 1))))


def tempo_minutes(a: Athlete, p: float) -> int:
    lo, hi = TEMPO_MINUTES[a.bucket]
    s = LEVEL_TEMPO_SCALE.get(a.level, 1.0)
    return max(TEMPO_MIN_MINUTES, round((lo + (hi - lo) * p) * s / 5) * 5)


def mp_run_minutes(a: Athlete, p: float) -> int:
    lo, hi = MP_RUN_MINUTES[a.bucket if a.bucket in MP_RUN_MINUTES else 21.0975]
    return max(TEMPO_MIN_MINUTES, round((lo + (hi - lo) * p) * LEVEL_TEMPO_SCALE.get(a.level, 1.0) / 5) * 5)


def quality_candidates(a: Athlete, kind: str, p: float, occ: int, phase: str, taper_step: int) -> list[Session]:
    """Candidate sessions of a kind from the most demanding rung (for progress p) down to the easiest: the scheduler takes the first that fits.
    `occ` is how many sessions of this kind the athlete has had, which rotates the session family so weeks don't repeat."""
    out: list[Session] = []
    if kind == "sharpener":
        return [sharpener_session(a, taper_step)]
    if kind == "benchmark":
        return [benchmark_session(a)]
    if kind == "tempo":
        top = tempo_minutes(a, p)
        variant = (None, 2, 3)[occ % 3] if top >= 20 else (None, 2)[occ % 2]          # continuous, 2 long reps, 3 shorter reps
        for m in range(top, TEMPO_MIN_MINUTES - 1, -5):
            out.append(tempo_session(a, m, variant if (variant and m >= 18) else False))
        if not out or out[-1].type != "tempo":
            out.append(tempo_session(a, TEMPO_MIN_MINUTES, False))
        return out
    if kind == "mp-run":
        top = mp_run_minutes(a, p)
        for m in range(top, TEMPO_MIN_MINUTES - 1, -5):
            if a.bucket >= 42:
                out.append(tempo_session(a, m, False, pace_key="marathon", label="marathon pace", title_word="Marathon-pace run"))
            else:
                out.append(tempo_session(a, m, False, pace_key="threshold", label="half-marathon", title_word="Half-marathon-pace run"))
        return out
    if kind == "fartlek":
        for i in range(climb(len(FARTLEK), p), -1, -1):
            out.append(fartlek_session(a, FARTLEK[i]))
        return out
    if kind == "race-intervals":
        lad = RACE_INTERVALS[a.bucket]
        for i in range(climb(len(lad), p), -1, -1):
            out.append(interval_session(a, lad[i], at_goal=True))
        return out
    family = ("ladder", "hills", "ladder", "pyramid")[occ % 4]                          # plain intervals rotate through four families
    if family == "hills":
        return [hill_session(a, HILLS[i]) for i in range(climb(len(HILLS), p), -1, -1)]
    if family == "pyramid":
        pyr = PYRAMIDS[a.bucket]
        return [pyramid_session(a, pyr[i]) for i in range(climb(len(pyr), p), -1, -1)] + [interval_session(a, INTERVALS[a.bucket][0])]
    lad = INTERVALS[a.bucket]
    top = min(len(lad) - 1, climb(len(lad), p) + (1 if occ % 4 == 2 else 0))          # the second ladder visit is one rung up
    return [interval_session(a, lad[i]) for i in range(top, -1, -1)]


def quality_plan(a: Athlete, phase: str, recovery: bool, n_slots: int, week_idx: int, prog: float, *, runwalk: bool, paced_long: bool) -> list[str]:
    """Which hard sessions this week gets (kinds, in order). A week with a paced long run has at most one other hard session."""
    if recovery or a.pain or n_slots <= 0:
        return []
    if phase in ("taper", "race"):
        return ["sharpener"]
    if a.level == "beginner":
        if runwalk or phase == "base":
            return []
        return ["fartlek"] if week_idx % 2 == 0 else ["tempo"]
    if phase == "base":
        if a.bucket >= 10 and week_idx >= 2 and week_idx % 2 == 1:
            return ["tempo"]                                  # half and marathon builds start threshold work in the base, as Daniels and Pfitzinger do
        return ["fartlek"]
    n = 1 if paced_long else n_slots
    if a.bucket in (5.0, 10.0):
        kinds = ["race-intervals", "tempo"] if phase == "peak" else (["intervals", "tempo"] if week_idx % 2 == 0 else ["tempo", "intervals"])
    elif a.bucket >= 42:
        # marathon: threshold and marathon-pace work lead; 5K-pace intervals are an occasional sharpener, never the main session near the race
        if phase == "peak":
            kinds = ["mp-run", "tempo"] if week_idx % 2 == 0 else ["tempo", "mp-run"]
        else:
            kinds = ["tempo", "intervals"] if week_idx % 3 == 0 else ["tempo", "mp-run"] if phase == "build" and week_idx % 3 == 2 else ["tempo", "tempo"]
            kinds = [kinds[0]] + [k for k in kinds[1:] if k != "tempo"] or kinds[:1]
    else:
        if phase == "peak":
            kinds = ["mp-run", "tempo"] if week_idx % 2 == 0 else ["tempo", "intervals"]
        else:
            kinds = ["tempo", "intervals"] if week_idx % 2 == 0 else ["intervals", "tempo"]
    if n == 1:
        return [kinds[week_idx % len(kinds)]]
    return kinds[:n]


# ------------------------------------------------------------------------------------------------------------------------
def run_walk_for(rung: tuple[int, int], minutes: float) -> tuple[int, int]:
    """A session must hold at least three run/walk cycles: a short session uses an earlier rung rather than one 15-minute run."""
    i = RUN_WALK.index(rung)
    while i > 0 and minutes * 60 / (RUN_WALK[i][0] + RUN_WALK[i][1]) < 3:
        i -= 1
    return RUN_WALK[i]


def run_walk_rung(a: Athlete, nth: int) -> tuple[int, int] | None:
    """Run/walk rung for the nth (0-based) non-recovery training week, or None once running is continuous (or never needed)."""
    if a.level != "beginner":
        return None
    start = next((r for lim, r in RUN_WALK_START_KM if a.baseline_km < lim), None)
    if start is None:
        return None
    i = start + nth
    return RUN_WALK[i] if i < len(RUN_WALK) else None


def build(ctx: dict) -> dict:
    a = parse(ctx)
    today = date.fromisoformat(ctx["earliest_workout_date"])
    blocked = set(ctx.get("blocked_dates") or [])
    weeks = ctx["skeleton"]
    race = a.race_date
    train_idx = [i for i, w in enumerate(weeks) if w["phase"] in ("base", "build", "peak") and not w["recovery"]]
    n_prog = max(1, len(train_idx) - 1)
    long_dates = set()
    for w in weeks:
        if w["long_run_km"] > 0 and a.avail.get(a.long_day, (False, 0))[0] and not w["race_week"]:
            d = date.fromisoformat(w["week_start"]) + timedelta(days=a.long_day)
            if today <= d < race and d.isoformat() not in blocked:
                long_dates.add(d)
    hard_dates: list[date] = []
    out: list[dict] = []
    paced_elig = paced_done = 0
    heavy: list[date] = []                       # dates of long runs with a pace block
    occ: dict[str, int] = {}
    nb = len(train_idx)
    bench_weeks = {train_idx[round(f * (nb - 1))] for f in BENCHMARK_SHARE} if nb >= 10 else ({train_idx[nb // 2]} if nb >= 6 else set())
    nth_train = 0
    taper_seen = 0
    first_week = weeks[0]["week_start"] if weeks else None
    for wi, w in enumerate(weeks):
        ws = date.fromisoformat(w["week_start"])
        days = [(i, ws + timedelta(days=i)) for i in range(7)
                if a.avail.get(i, (False, 0))[0] and today <= ws + timedelta(days=i) <= race and (ws + timedelta(days=i)).isoformat() not in blocked]
        phase = "taper" if w["phase"] == "race" else w["phase"]
        if w["race_week"]:
            out += race_week(a, w, ws, days, hard_dates)
            continue
        target = w["target_km"] - w.get("already_done_km", 0.0)
        if target <= 0 or not days:
            continue
        long_d = next((d for i, d in days if i == a.long_day), None) if w["long_run_km"] > 0 else None
        long_km = float(w["long_run_km"]) if long_d else 0.0
        training = w["phase"] in ("base", "build", "peak")
        rec = bool(w["recovery"])
        rw = run_walk_rung(a, nth_train) if training and not rec else (run_walk_rung(a, max(0, nth_train - 1)) if training else None)
        if training and not rec:
            nth_train += 1
        prog = (train_idx.index(wi) / n_prog) if wi in train_idx else (min((x for x in train_idx if x > wi), default=wi) and (sum(1 for x in train_idx if x < wi) / n_prog))
        prog = min(1.0, prog)
        limit = K.quality_limit(a.level, phase, rec, a.run_days)
        # --- long run: plain, or with a pace block in the specific weeks of a marathon / half-marathon build ---
        paced = False
        if long_d and phase in ("build", "peak") and not rec and prog >= PACED_LONG_FROM and a.bucket >= 21 and a.level != "beginner" and not a.pain \
                and rw is None and long_km >= max(8.0, 0.7 * a.peak_long_km) and not _near(long_d, hard_dates, 2) and window_ok(long_d, hard_dates):
            paced = paced_elig % 2 == 0                    # every other eligible long run carries a pace block
            paced_elig += 1
        trans = training and rw is None and nth_train >= 2 and run_walk_rung(a, nth_train - 2) is not None
        just_ended = training and nth_train >= 2 and run_walk_rung(a, nth_train - 2) is not None      # a week of continuous running before the first hard session
        kinds = quality_plan(a, phase, rec, limit, wi, prog, runwalk=rw is not None or just_ended, paced_long=paced)
        if wi in bench_weeks and kinds and not paced and not rec and a.level != "beginner":
            kinds = ["benchmark"] + kinds[1:]
        if paced:
            hard_dates.append(long_d)
            heavy.append(long_d)
            kinds = ["tempo" if k == "mp-run" else k for k in kinds]        # no marathon-pace run in the week of a marathon-pace long run
        # --- the week: hard sessions, long run, easy days. If its days can't carry the km, give the hard days longer warm-ups and let easy
        # runs come closer to the long run's length before settling for a lighter week ---
        base_wu = WARMUP_MIN.get(a.level, 10)
        best = None
        for wu, share, thin in ((base_wu, EASY_MAX_OF_LONG, True), (base_wu, 0.95, True), (base_wu, 1.0, True), (base_wu, 1.0, False),
                                (base_wu + 2, 1.0, False), (base_wu + 4, 1.0, False), (base_wu + 6, 1.0, False)):
            a.wu_override = wu
            sessions, used, q_days = place_quality(a, days, kinds, long_dates, hard_dates, race, target, prog, occ, phase, taper_seen, heavy)
            lk = round(long_km, 1) if long_d else 0.0
            long_s = None
            if long_d:
                if paced:
                    ladder = MP_BLOCK_KM if a.bucket >= 42 else HMP_BLOCK_KM
                    block = min(ladder[min(paced_done, len(ladder) - 1)], PACED_LONG_MAX_SHARE.get(a.bucket, 0.4) * lk)
                    if block >= 3:
                        long_s = paced_long_session(a, lk, round(block))
                if long_s is None:
                    long_s = easy_session(a, lk, kind="long", run_walk=rw, transition=trans)
            taken = set(sessions) | ({long_d} if long_d else set())
            easy_days = [(i, d) for i, d in days if d not in taken]
            if rw is not None and thin:
                easy_days = thin_for_run_walk(easy_days, taken)
            remaining = target - round(lk, 1) - sum(round(x.km, 1) for x in sessions.values())
            near_hard = frozenset(d for _, d in easy_days if any(abs((d - x).days) <= 1 for x in list(sessions) + list(long_dates) + hard_dates))
            alloc = spread_easy(a, easy_days, remaining, lk, share=share, avoid=near_hard,
                                medium=(training and not rec and phase in ("build", "peak") and a.bucket >= 21 and rw is None))
            gap = remaining - sum(km for km, _ in alloc)
            cand = (gap, sessions, long_s, easy_days, alloc, used)
            if best is None or gap < best[0] - 0.2:
                best = cand
            if gap <= 0.03 * target or not sessions:
                if gap <= 0.03 * target:
                    best = cand
                    break
        a.wu_override = None
        _, sessions, long_s, easy_days, alloc, used = best
        for k in used.values():
            occ[k] = occ.get(k, 0) + 1
        if long_s is not None and long_s.hard_km and long_d:
            paced_done += 1
        if phase == "taper":
            taper_seen += 1
        hard_dates.extend(sessions)
        stride_days = pick_stride_days(easy_days, alloc, long_dates, hard_dates, 2 if phase == "base" else 1) if (training and not rec and rw is None) else set()
        if long_s:
            out.append(row(long_d, long_s))
        for d, ss in sessions.items():
            out.append(row(d, ss))
        for (i, d), (km, kind) in zip(easy_days, alloc):
            if km <= 0:
                continue
            out.append(row(d, easy_session(a, km, strides=d in stride_days, kind=kind, run_walk=rw, transition=trans)))
    if not any(x["type"] == "race" for x in out):
        out.append(row(race, race_session(a)))
    out.sort(key=lambda x: x["date"])
    return {"summary": "", "workouts": out}


def place_quality(a: Athlete, days, kinds, long_dates, hard_dates, race: date, target: float, prog: float, occ: dict, phase: str, taper_seen: int,
                  heavy: list[date] = ()):
    """Pick the days for this week's hard sessions and the hardest rung of each that fits its day and the week's caps. Returns
    ({date: session}, {date: kind}, days)."""
    q_days = pick_quality_days(a, days, kinds, long_dates, hard_dates, race, taper=phase == "taper", heavy=heavy)
    sessions: dict[date, Session] = {}
    used: dict[date, str] = {}
    seen = dict(occ)
    for d, kind in zip(q_days, kinds):
        avail_min = a.avail[d.weekday()][1]
        cands = quality_candidates(a, kind, prog, seen.get(kind, 0), phase, taper_seen)
        budget = QUALITY_BUDGET * target / max(1, len(q_days))
        tkind = "bench" if kind == "benchmark" else ("tempo" if kind in ("tempo", "mp-run", "sharpener") else "intervals")
        chosen = next((x for x in cands if fits(a, x, target, avail_min, budget, tkind)), None)
        if chosen is None and kind != "benchmark":        # nothing meets the share caps: the gentlest rung that still fits the day (the caps are guidance, the day is a fact)
            cap_min = min(avail_min, K.run_cap_minutes(a.level, "tempo", a.goal_km)) if avail_min else K.run_cap_minutes(a.level, "tempo", a.goal_km)
            chosen = next((x for x in reversed(cands) if x.minutes <= cap_min - 1 and x.km <= budget + 0.05), None)
        if chosen is not None:
            sessions[d] = chosen
            used[d] = kind
            seen[kind] = seen.get(kind, 0) + 1
    return sessions, used, q_days


def window_ok(d: date, dates) -> bool:
    """Would a hard effort on `d` keep every 7-day window to at most two hard efforts (tempo, intervals, paced long run)?"""
    allx = sorted(list(dates) + [d])
    return all(sum(1 for y in allx if 0 <= (y - x).days <= 6) <= 2 for x in allx)


def _near(d: date, dates, gap: int) -> bool:
    return any(abs((d - x).days) < gap for x in dates)


def pick_quality_days(a: Athlete, days, kinds: list[str], long_dates: set[date], hard_dates: list[date], race: date, *, taper: bool,
                      heavy: list[date] = ()) -> list[date]:
    chosen: list[date] = []
    if not kinds:
        return chosen
    for _ in kinds:
        best, best_score = None, -1
        for i, d in days:
            if d in long_dates or d in chosen:
                continue
            if _near(d, long_dates, 2) or _near(d, hard_dates, 2) or _near(d, chosen, 2) or not window_ok(d, list(hard_dates) + chosen) \
                    or _near(d, heavy, 3):               # a long run with a pace block needs three easy days either side of any other hard session
                continue
            to_race = (race - d).days
            if 0 <= to_race <= (3 if taper else 2):
                continue
            score = min(min((abs((d - x).days) for x in long_dates), default=3), 3) * 10 + WEEKDAY_PREFERENCE[i]
            if score > best_score:
                best, best_score = d, score
        if best is not None:
            chosen.append(best)
    return sorted(chosen)


def thin_for_run_walk(easy_days, fixed: set[date]):
    """Run/walk beginners run three days a week with a rest day between where the week allows: keep the easy days that are not next to another run."""
    keep, runs = [], set(fixed)
    for i, d in easy_days:
        if len(runs) >= 3:
            break
        if any(abs((d - r).days) == 1 for r in runs) and len(easy_days) > 2:
            continue
        keep.append((i, d))
        runs.add(d)
    if len(runs) < 3:                                       # not enough spaced days: take adjacent ones rather than fewer runs
        for i, d in easy_days:
            if (i, d) not in keep and len(runs) < 3:
                keep.append((i, d))
                runs.add(d)
    return sorted(keep, key=lambda x: x[1])


def easy_cap_km(a: Athlete, minutes: int, long_km: float, share: float = EASY_MAX_OF_LONG) -> float:
    m = K.run_cap_minutes(a.level, "easy", a.goal_km)
    if minutes:
        m = min(m, minutes)
    cap = (m - 1) * 60 / a.easy
    if long_km:
        cap = min(cap, share * long_km)
    return cap


def spread_easy(a: Athlete, easy_days, remaining: float, long_km: float, *, medium: bool, share: float = EASY_MAX_OF_LONG, avoid: frozenset = frozenset()) -> list[tuple[float, str]]:
    """Share `remaining` km over the easy days (one of them a medium-long run in marathon and half builds), each within its time cap."""
    n = len(easy_days)
    if n == 0 or remaining <= 0:
        return [(0.0, "easy")] * n
    caps = [easy_cap_km(a, a.avail[d.weekday()][1], long_km, share) for _, d in easy_days]
    weights = [1.0] * n
    kinds = ["easy"] * n
    ok = [k for k in range(n) if easy_days[k][1] not in avoid]
    if medium and n >= 2 and ok:                      # the medium-long run is never the day after the long run or next to a hard day
        j = max(ok, key=lambda k: caps[k])
        weights[j], kinds[j] = 1.45, "medium"
    floor = MIN_EASY_KM.get(a.level, 3.0)
    active = [k for k in range(n)]
    out = [0.0] * n
    left = remaining
    while active:
        tw = sum(weights[k] for k in active)
        share = {k: left * weights[k] / tw for k in active}
        over = [k for k in active if share[k] > caps[k]]
        if not over:
            for k in active:
                out[k] = share[k]
            break
        for k in over:
            out[k] = caps[k]
            left -= caps[k]
            active.remove(k)
    # days that would be shorter than the floor: drop them and give their km to the rest
    for _ in range(n):
        short = [k for k in range(n) if 0 < out[k] < floor]
        if not short or sum(1 for x in out if x > 0) <= 1:
            break
        k = min(short, key=lambda x: out[x])
        give = out[k]
        rest = [x for x in range(n) if x != k and out[x] > 0 and out[x] < caps[x]]
        room = sum(caps[x] - out[x] for x in rest)
        if room < give:                                    # the others can't carry it: three short runs beat two capped ones
            break
        out[k] = 0.0
        for x in rest:
            out[x] = min(caps[x], out[x] + give * (caps[x] - out[x]) / room)
    res = []
    for k in range(n):
        v = round(out[k], 1)
        res.append((min(v, round(caps[k], 1)), kinds[k] if v > 0 else "easy"))
    return res


def pick_stride_days(easy_days, alloc, long_dates, hard_dates, n: int) -> set[date]:
    cand = [d for (i, d), (km, kind) in zip(easy_days, alloc) if km >= 4 and kind == "easy"]
    cand.sort(key=lambda d: -min((abs((d - x).days) for x in hard_dates), default=9))      # strides go on easy days, ideally away from the hard ones
    return set(cand[:n])


def row(d: date, s: Session) -> dict:
    return {"date": d.isoformat(), "type": s.type, "title": s.title, "description": s.description, "distance_km": round(s.km, 1),
            "duration_min": max(1, round(s.minutes)), "pace_guidance": s.pace_guidance}


def race_session(a: Athlete) -> Session:
    mins = (a.goal_time_s / 60) if a.goal_time_s else a.goal_km * a.easy * 0.92 / 60
    name = K.goal_name(a.goal_km)
    cue = goal_pace_text(a) if a.goal_pace else "even effort, start easy"
    fuel = " Fuel and drink as you practised." if a.bucket >= 21 else ""
    return Session("race", f"Race day: {name}", (f"Race day. Start a touch slower than goal pace for the first {K.fmt_dist(min(3.0, a.goal_km / 5), a.units)}, settle into {cue}, "
                    f"and save something for the last quarter.{fuel}" if a.goal_pace else
                    f"Race day. Start easy for the first {K.fmt_dist(min(3.0, a.goal_km / 5), a.units)}, then settle into a strong, even effort you could hold to the end, "
                    f"and save something for the last quarter.{fuel}"), a.goal_km, mins, cue)


def race_week(a: Athlete, w: dict, ws: date, days, hard_dates: list[date]) -> list[dict]:
    race = a.race_date
    out = [row(race, race_session(a))]
    before = [(i, d) for i, d in days if d < race]
    if not before:
        return out
    target = max(0.0, w["target_km"] - a.goal_km)
    level = a.level
    # shakeout the day before (or the last available day before it), an easy run earlier in the week, and a goal-pace touch 3+ days out
    last = before[-1]
    plan: list[tuple[date, str]] = [(last[1], "shakeout")]
    rest = before[:-1]
    touch = None
    if level != "beginner" and not a.pain and K.quality_limit(level, "taper", False, a.run_days) >= 1:
        touch = next(((i, d) for i, d in reversed(rest) if (race - d).days >= 3 and not _near(d, hard_dates, 2) and window_ok(d, hard_dates) and a.avail[i][1] >= 40), None)
    if touch:
        plan.append((touch[1], "touch"))
        rest = [x for x in rest if x[1] != touch[1]]
    if rest and target > 12:
        plan.append((rest[0][1], "easy"))
    elif rest and target > 5 and len(plan) < 2:
        plan.append((rest[-1][1], "easy"))
    budget = target if target > 0 else 8.0
    km_by = {"shakeout": min(5.0, max(3.0, 0.2 * budget)), "touch": 0.0, "easy": 0.0}
    sessions: dict[date, Session] = {}
    for d, kind in plan:
        if kind == "touch":
            s = tempo_like_goal(a, 2, 1000) if a.bucket >= 10 else interval_session(a, (4, 400, 90), at_goal=True)
            while s.km > 0.55 * budget and len(s.description) < 0:      # (kept simple: the touch is already brief)
                break
            s.title = "Goal-pace touch" + s.title[s.title.index(" "):] if False else s.title
            sessions[d] = s
        elif kind == "shakeout":
            sessions[d] = easy_session(a, km_by["shakeout"], strides=True, title="Shakeout run + strides")
            sessions[d].description = f"Very easy 20–25 min, then {STRIDES}. The aim is loose, fresh legs. Nothing hard."
        else:
            left = max(3.0, budget - sum(s.km for s in sessions.values()) - 0.0)
            sessions[d] = easy_session(a, min(left, easy_cap_km(a, a.avail[d.weekday()][1], 0.0), 8.0), title="Easy run")
    for d in sessions:
        out.append(row(d, sessions[d]))
        if sessions[d].type == "tempo" or sessions[d].type == "intervals":
            hard_dates.append(d)
    return out


# ------------------------------------------------------------------------------------------------------------------------
def summary_text(ctx: dict) -> str:
    a = ctx["athlete"]
    weeks = ctx["skeleton"]
    n = len(weeks)
    peak = max((w["target_km"] for w in weeks), default=0)
    goal = a["goal"] or K.goal_name(a["goal_km"])
    first_name = (a.get("name") or "").split(" ")[0]
    greet = f"{first_name}, here" if first_name else "Here"
    lines = [f"{greet} is your {n}-week plan for the {goal} on {a['race_date']}. It builds your weekly distance and long run step by step, "
             f"with an easier week every few weeks to absorb the training, then tapers so you arrive fresh."]
    lines.append("Most of your running is easy and conversational. Hard sessions are limited and never next to your long run: threshold runs and "
                 "intervals get a little longer or faster as the weeks go on, and the final weeks add work at your goal pace.")
    return " ".join(lines)


class BuilderCoach:
    """Drop-in for `llm.Coach`: plans come from code, not from the language model."""

    def plan(self, ctx: dict, *, mode: str, feedback: list[str] | None = None) -> dict:
        out = build(ctx)
        out["summary"] = summary_text(ctx)
        return out
