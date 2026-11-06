"""Scenario suite: plans are checked against the researched rules (docs/RESEARCH.md) across many athletes.

No network and no language model: the grid runs the deterministic weekly skeleton, the adversarial tests feed deliberately
bad plans through the checker, and the end-to-end tests run the real service with a rule-following reference coach.
"""
import itertools
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from coach import knowledge as K
from coach.config import Settings
from coach.db import DB
from coach.models import DAYS, DayAvail, Profile
from coach.plancheck import mentions_pain, sanitize
from coach.service import CoachService
from coach.skeleton import build_skeleton_info, monday
from coach.web import create_app
from test_core import FakeStrava, signup, wait_job

TODAY = date(2026, 11, 4)  # a Wednesday
PACE = 390.0
PHASE_ORDER = ["base", "build", "peak", "taper", "race"]


def prof(goal_km=5.0, weeks=12, level="intermediate", baseline=25.0, days=("tue", "thu", "sat", "sun"), minutes=60, long_minutes=120,
         long_day="sun", goal_time_s=None):
    return Profile(
        goal_label=K.goal_name(goal_km), goal_km=goal_km, race_date=TODAY + timedelta(weeks=weeks), baseline_km=baseline, experience=level,
        availability={d: DayAvail(available=d in days, minutes=long_minutes if d == long_day else minutes) for d in DAYS},
        long_run_day=long_day, goal_time_s=goal_time_s)


def skeleton_for(p, baseline=None, minutes_total=None, source="stated"):
    return build_skeleton_info(
        plan_start=TODAY, today=TODAY, race_date=p.race_date, goal_km=p.goal_km, baseline_km=p.baseline_km if baseline is None else baseline,
        experience=p.experience, weekly_minutes=p.weekly_minutes if minutes_total is None else minutes_total, long_day_minutes=p.long_day_minutes,
        easy_pace_s_km=None, run_days=sum(a.available for a in p.availability.values()), baseline_source=source)


# =============================== layer 1: the weekly skeleton across ~2,700 athletes ===============================
GOALS = [5.0, 10.0, 21.0975, 42.195, 15.0]
WEEKS = [4, 8, 12, 16, 20, 24]
BASELINES = [0, 8, 15, 25, 40, 60]
SCHEDULES = [(90, 3, 45), (180, 3, 75), (240, 4, 90), (360, 5, 120), (480, 6, 180)]  # (minutes/week, run days, long-run-day minutes)


def skeleton_problems(goal, level, n_weeks, base, minutes, days, long_min) -> list[str]:
    race = TODAY + timedelta(weeks=n_weeks)
    weeks, o = build_skeleton_info(plan_start=TODAY, today=TODAY, race_date=race, goal_km=goal, baseline_km=base, experience=level,
                                   weekly_minutes=minutes, long_day_minutes=long_min, easy_pace_s_km=None, run_days=days)
    out, who = [], f"{K.goal_name(goal)}/{level}/{n_weeks}w/base{base}/{minutes}min"
    low, target, high = K.peak_range(goal, level)
    cur = max(base, K.MIN_BASE_KM)
    time_cap = minutes / (PACE / 60)
    train = [w for w in weeks if w.phase in ("base", "build", "peak")]

    if weeks[0].week_start != monday(TODAY) or not weeks[-1].race_week or weeks[-1].week_start != monday(race):
        out.append(f"{who}: weeks don't span today..race")
    order = [PHASE_ORDER.index(w.phase) for w in weeks]
    if order != sorted(order):
        out.append(f"{who}: phases out of order {[w.phase for w in weeks]}")
    if weeks[0].target_km < cur - 0.5:
        out.append(f"{who}: starts below the athlete's current volume ({weeks[0].target_km} < {cur})")

    last_normal = None
    for w in train:
        if w.recovery:
            if last_normal is not None and w.target_km > 0.8 * last_normal + 0.5:
                out.append(f"{who}: recovery week {w.week_start} not reduced enough")
            continue
        if last_normal is not None and w.target_km > last_normal + K.max_weekly_increase_km(last_normal, level) + 0.75:
            out.append(f"{who}: week {w.week_start} jumps {last_normal}->{w.target_km} (limit {K.max_weekly_increase_km(last_normal, level):.1f})")
        last_normal = w.target_km
        if w.target_km > max(time_cap, cur) + 0.75:
            out.append(f"{who}: week {w.week_start} {w.target_km} km exceeds the time budget {time_cap:.1f}")

    if train:
        peak = max(w.target_km for w in train)
        if peak > max(high, cur) + 0.75:
            out.append(f"{who}: peak {peak} above published high {high}")
        if peak < weeks[0].target_km - 0.5:
            out.append(f"{who}: peak {peak} below start {weeks[0].target_km}")
        if o.limit == "goal" and abs(peak - target) > 1.0:
            out.append(f"{who}: limit=goal but peak {peak} != target {target}")
        # time-limited: the peak sits at the schedule's capacity, or at the athlete's current volume if that is already higher
        if o.limit == "time" and not (time_cap < target - 0.5 and abs(peak - max(time_cap, cur)) <= 1.0):
            out.append(f"{who}: limit=time inconsistent (peak {peak}, cap {time_cap:.1f}, target {target})")
        if o.limit == "ramp" and peak >= min(target, time_cap) - 0.25:
            out.append(f"{who}: limit=ramp but peak reached the cap")
        if o.limit == "none" and cur < target and train:
            out.append(f"{who}: limit=none though baseline {cur} < target {target}")
        if weeks[-1].target_km > max(0.65 * peak, goal + 6) + 0.75:
            out.append(f"{who}: race week {weeks[-1].target_km} too high vs peak {peak}")
    for w in weeks:
        if w.race_week:
            continue
        caps = [K.long_run_cap_km(goal, level) + 0.25, K.long_run_max_minutes(goal) * 60 / PACE + 0.25,
                K.long_run_share(w.target_km, days, goal) * w.target_km + 0.25]
        if long_min:
            caps.append(long_min * 60 / PACE + 0.25)
        if w.long_run_km > min(caps):
            out.append(f"{who}: long run {w.long_run_km} exceeds a limit {min(caps):.1f} in week {w.week_start}")
    return out


def test_skeleton_invariants_hold_across_thousands_of_athletes():
    problems, n = [], 0
    for goal, level, wk, base, (mins, days, lm) in itertools.product(GOALS, K.LEVELS, WEEKS, BASELINES, SCHEDULES):
        problems += skeleton_problems(goal, level, wk, base, mins, days, lm)
        n += 1
    assert n == 2700
    assert not problems, f"{len(problems)} violations, first 8:\n" + "\n".join(problems[:8])


# --- the reported failure, pinned down ---
@pytest.mark.parametrize("level,days,mins", [("intermediate", 4, 240), ("intermediate", 5, 300), ("advanced", 5, 360)])
def test_5k_plans_for_regular_runners_are_not_tiny(level, days, mins):
    """Regression: a 5K plan peaked at ~12-16 km/week. Published 5K plans for these levels peak around 30-48 km."""
    base = K.assumed_baseline_km(level)
    _, o = build_skeleton_info(plan_start=TODAY, today=TODAY, race_date=TODAY + timedelta(weeks=8), goal_km=5.0, baseline_km=base,
                               experience=level, weekly_minutes=mins, long_day_minutes=90, easy_pace_s_km=None, run_days=days)
    lo, target, hi = K.peak_range(5.0, level)
    assert o.peak_km >= min(target, o.time_cap_km) * 0.9
    assert o.peak_km >= 30, (level, o.peak_km)


def test_the_old_behaviour_would_have_failed_this_check():
    # The version-1 rule (8 km floor, +8%/week for 8 weeks) tops out at ~15 km: below every published intermediate range.
    level = 8.0
    for _ in range(8):
        level *= 1.08
    assert level < K.peak_range(5.0, "intermediate")[0]


def test_short_runway_and_tiny_schedules_are_reported_not_hidden():
    p = prof(goal_km=21.0975, weeks=6, level="beginner", baseline=10)
    _, o = skeleton_for(p)
    assert o.limit == "ramp" and o.weeks_to_race < o.min_weeks
    text = o.athlete_text("half marathon", "beginner")
    assert "enough weeks" in text and "compressed" in text
    _, o2 = skeleton_for(prof(days=("sun",), minutes=40, long_minutes=40, baseline=25))
    assert o2.limit == "time" and "available running time" in o2.athlete_text("5K", "intermediate")


# ====================== layer 2: bad plans through the checker (what the model might get wrong) ====================
def all_days_profile(level="intermediate", goal_km=5.0, weeks=12):
    return prof(goal_km=goal_km, weeks=weeks, level=level, days=DAYS, minutes=120, long_minutes=180)


def wk_workout(d: date, typ: str, km: float, title="x"):
    return {"date": d.isoformat(), "type": typ, "title": title, "description": "", "distance_km": km, "duration_min": round(km * PACE / 60), "pace_guidance": ""}


def run_sanitize(p, raw, weeks, **kw):
    args = dict(profile=p, weeks=weeks, today=TODAY, blocked_dates=set(), done_km_by_week={}, pace_s_km=PACE, level=p.experience)
    args.update(kw)
    return sanitize(raw, **args)


def find_week(weeks, **attrs):
    return next(w for w in weeks if all(getattr(w, k) == v for k, v in attrs.items()))


def quality_of(clean, week_start):
    return sorted(w["date"] for w in clean if w["week_start"] == week_start.isoformat() and w["type"] in K.QUALITY_TYPES)


def test_back_to_back_and_long_run_adjacent_hard_sessions_are_downgraded():
    p = all_days_profile()
    weeks, _ = skeleton_for(p)
    wk = next(w for w in weeks if w.phase == "build" and not w.recovery)
    d = lambda i: wk.week_start + timedelta(days=i)  # noqa: E731
    raw = [wk_workout(d(0), "easy", 6), wk_workout(d(1), "tempo", 8), wk_workout(d(2), "intervals", 8), wk_workout(d(3), "intervals", 8),
           wk_workout(d(4), "easy", 5), wk_workout(d(5), "tempo", 8), wk_workout(d(6), "long", 11)]
    clean, notes = run_sanitize(p, raw, weeks)
    q = [date.fromisoformat(x) for x in quality_of(clean, wk.week_start)]
    long_day = next(date.fromisoformat(w["date"]) for w in clean if w["type"] == "long")
    assert 1 <= len(q) <= K.quality_limit("intermediate", "build", False, 7)
    assert all(b - a >= timedelta(days=2) for a, b in zip(q, q[1:]))                 # an easy day between hard sessions
    assert all(abs((x - long_day).days) >= 2 for x in q)                              # none the day before/after the long run
    assert any("changed to an easy run" in n for n in notes)
    assert all(w["type"] == "easy" for w in clean if w["date"] in (d(2).isoformat(), d(5).isoformat()))


def test_quality_limits_by_level_are_enforced():
    for level, want in (("beginner", 1), ("intermediate", 2), ("advanced", 2)):
        p = all_days_profile(level)
        weeks, _ = skeleton_for(p)
        wk = next(w for w in weeks if w.phase == "build" and not w.recovery)
        d = lambda i: wk.week_start + timedelta(days=i)  # noqa: E731
        raw = [wk_workout(d(0), "tempo", 7), wk_workout(d(2), "intervals", 7), wk_workout(d(4), "tempo", 7), wk_workout(d(6), "long", 9)]
        clean, _ = run_sanitize(p, raw, weeks)
        assert len(quality_of(clean, wk.week_start)) == want, level


def test_recovery_weeks_contain_no_hard_sessions():
    p = all_days_profile()
    weeks, _ = skeleton_for(p)
    wk = next(w for w in weeks if w.recovery)
    d = lambda i: wk.week_start + timedelta(days=i)  # noqa: E731
    clean, notes = run_sanitize(p, [wk_workout(d(1), "tempo", 6), wk_workout(d(3), "intervals", 6), wk_workout(d(6), "long", 8)], weeks)
    assert quality_of(clean, wk.week_start) == [] and any("recovery week" in n for n in notes)


def test_long_run_is_capped_by_size_and_by_growth_over_the_last_month():
    p = all_days_profile(level="beginner")
    weeks, _ = skeleton_for(p)
    wk = weeks[1]
    sunday = wk.week_start + timedelta(days=6)
    clean, notes = run_sanitize(p, [wk_workout(sunday, "long", 20)], weeks, longest_recent_km=6.0)
    long_run = next(w for w in clean if w["type"] == "long")
    assert long_run["distance_km"] <= max(6.0 * 1.10, 6.0 + 2.5) + 0.05                  # growth limit
    assert long_run["distance_km"] <= K.long_run_cap_km(5.0, "beginner") * 1.05          # event cap
    assert long_run["duration_min"] < 20 * PACE / 60 and any("long run reduced" in n for n in notes)
    # without history, the week's own long-run target (plus slack) bounds it
    clean2, _ = run_sanitize(p, [wk_workout(sunday, "long", 20)], weeks)
    assert next(w for w in clean2 if w["type"] == "long")["distance_km"] <= wk.long_run_km * 1.10 + 0.05


def test_marathon_long_runs_are_allowed_to_be_long():
    p = all_days_profile(level="intermediate", goal_km=42.195, weeks=20)
    weeks, _ = skeleton_for(p)
    peak = max((w for w in weeks if w.phase == "peak"), key=lambda w: w.long_run_km)
    sunday = peak.week_start + timedelta(days=6)
    clean, _ = run_sanitize(p, [wk_workout(sunday, "long", peak.long_run_km)], weeks, longest_recent_km=peak.long_run_km)
    assert next(w for w in clean if w["type"] == "long")["distance_km"] == peak.long_run_km
    assert peak.long_run_km >= 25


def test_hard_sessions_cannot_swallow_a_thin_week():
    p = all_days_profile()
    weeks, _ = skeleton_for(p, baseline=12)                       # a low-volume week
    wk = next(w for w in weeks if w.phase == "build" and not w.recovery)
    d = lambda i: wk.week_start + timedelta(days=i)  # noqa: E731
    raw = [wk_workout(d(1), "tempo", 9), wk_workout(d(3), "intervals", 9), wk_workout(d(0), "easy", 3), wk_workout(d(4), "easy", 3),
           wk_workout(d(6), "long", 5)]
    clean, notes = run_sanitize(p, raw, weeks)
    hard = sum(w["distance_km"] for w in clean if w["type"] in K.QUALITY_TYPES and w["week_start"] == wk.week_start.isoformat())
    assert hard <= K.HARD_SHARE_MAX * wk.target_km + 0.5
    assert any("hard sessions shortened" in n for n in notes)
    total = sum(w["distance_km"] for w in clean if w["week_start"] == wk.week_start.isoformat())
    assert total >= wk.target_km * 0.85                          # the week's volume is restored with easy running


def test_under_delivering_weeks_are_topped_up_when_the_schedule_allows():
    p = all_days_profile()                                         # plenty of minutes on every day
    weeks, _ = skeleton_for(p)
    wk = weeks[2]
    d = lambda i: wk.week_start + timedelta(days=i)  # noqa: E731
    thin = [wk_workout(d(i), "easy", 3) for i in (0, 1, 3, 4)] + [wk_workout(d(6), "long", wk.long_run_km)]
    clean, notes = run_sanitize(p, thin, weeks)
    total = sum(w["distance_km"] for w in clean if w["week_start"] == wk.week_start.isoformat())
    assert total >= wk.target_km * 0.85, (total, wk.target_km)
    assert total <= wk.target_km * 1.10
    assert any("lengthened" in n for n in notes)
    assert all(w["distance_km"] <= wk.long_run_km for w in clean if w["type"] == "easy")      # easy runs never exceed the long run


def test_top_up_respects_each_days_available_minutes():
    p = prof(days=("tue", "thu", "sat", "sun"), minutes=30, long_minutes=60, baseline=25)        # only 30-minute weekdays
    weeks, _ = skeleton_for(p)
    wk = weeks[2]
    d = lambda i: wk.week_start + timedelta(days=i)  # noqa: E731
    raw = [wk_workout(d(1), "easy", 3), wk_workout(d(3), "easy", 3), wk_workout(d(5), "easy", 3), wk_workout(d(6), "long", 5)]
    clean, notes = run_sanitize(p, raw, weeks)
    for w in clean:
        cap = p.availability[DAYS[date.fromisoformat(w["date"]).weekday()]].minutes * 1.1
        assert w["duration_min"] <= cap + 1, w
    assert any("limit it" in n for n in notes)                    # and it tells us the schedule is the constraint


def test_over_long_weeks_are_scaled_down():
    p = all_days_profile()
    weeks, _ = skeleton_for(p)
    wk = weeks[1]
    d = lambda i: wk.week_start + timedelta(days=i)  # noqa: E731
    raw = [wk_workout(d(i), "easy", 12) for i in range(6)] + [wk_workout(d(6), "long", 15)]
    clean, _ = run_sanitize(p, raw, weeks)
    assert sum(w["distance_km"] for w in clean if w["week_start"] == wk.week_start.isoformat()) <= wk.target_km * 1.10 + 0.5


def test_pain_in_a_check_in_suppresses_hard_sessions_for_a_week():
    assert mentions_pain("my left calf is really sore") and mentions_pain("some knee pain after Sunday") and not mentions_pain("felt great, legs fresh")
    p = all_days_profile()
    weeks, _ = skeleton_for(p)
    thu = TODAY + timedelta(days=1)
    next_wk = weeks[1]
    later = next_wk.week_start + timedelta(days=1)
    raw = [wk_workout(thu, "tempo", 7), wk_workout(later, "tempo", 7)]
    clean, notes = run_sanitize(p, raw, weeks, suppress_quality_until=TODAY + timedelta(days=7))
    types = {w["date"]: w["type"] for w in clean}
    assert types[thu.isoformat()] == "easy" and any("pain or injury" in n for n in notes)
    assert types[later.isoformat()] in ("tempo", "easy")          # beyond the window the normal rules apply


def test_a_well_formed_plan_passes_through_without_any_repairs():
    p = all_days_profile()
    weeks, o = skeleton_for(p)
    clean, notes = run_sanitize(p, reference_plan(p, weeks, TODAY), weeks)
    assert notes == [], notes
    assert quality_of(clean, weeks[2].week_start)                                     # and it does contain hard sessions
    total = sum(w["distance_km"] for w in clean if w["week_start"] == weeks[3].week_start.isoformat())
    assert weeks[3].target_km * 0.85 <= total <= weeks[3].target_km * 1.10


# ======================= layer 3: the real service, with a reference coach that follows the rules ===================
def reference_plan(p, weeks, earliest, done_km_this_week=0.0):
    """What a good coach returns for this skeleton: long run on the long-run day, hard sessions spaced and off the long-run
    flank, the rest easy, volume ≈ 97% of target."""
    out = []
    avail = {i: a for i, a in enumerate(p.availability[d] for d in DAYS)}
    long_idx = DAYS.index(p.long_run_day)
    run_days = sum(a.available for a in p.availability.values())
    for wk in weeks:
        days = [(i, wk.week_start + timedelta(days=i)) for i in range(7) if avail[i].available and wk.week_start + timedelta(days=i) >= earliest]
        if wk.race_week:
            for i, d in days:
                if d <= p.race_date - timedelta(days=2):
                    out.append(wk_workout(d, "easy", 4)); break
            out.append(wk_workout(p.race_date, "race", p.goal_km))
            continue
        target = wk.target_km - (done_km_this_week if wk.week_start == monday(earliest) else 0.0)
        long_d = next((d for i, d in days if i == long_idx), None)
        limit = K.quality_limit(p.experience, wk.phase, wk.recovery, run_days)
        q_days, kinds = [], itertools.cycle(["tempo", "intervals"])
        for i, d in days:
            if len(q_days) >= limit or i == long_idx:
                continue
            if long_d and abs((d - long_d).days) < 2:
                continue
            if any(abs((d - x).days) < 2 for x in q_days):
                continue
            q_days.append(d)
        q_km = min(8.0, 0.22 * wk.target_km)
        long_km = wk.long_run_km if (long_d and wk.long_run_km > 0) else 0.0
        easy_days = [d for i, d in days if d != long_d and d not in q_days]
        remaining = 0.97 * target - long_km - q_km * len(q_days)
        easy_km = max(2.0, remaining / max(1, len(easy_days))) if easy_days else 0.0
        if long_km:
            out.append(wk_workout(long_d, "long", long_km))
        for d, kind in zip(q_days, kinds):
            out.append(wk_workout(d, kind, q_km, title=f"{kind.title()} session"))
        for d in easy_days:
            out.append(wk_workout(d, "easy", round(easy_km, 1)))
    out.append(wk_workout(p.race_date, "race", p.goal_km)) if not any(w["type"] == "race" for w in out) else None
    return out


class ReferenceCoach:
    """Rule-following stand-in for the language model; records what it was asked."""
    def __init__(self):
        self.calls = []

    def plan(self, ctx, *, mode, feedback=None):
        self.calls.append((mode, ctx, feedback))
        p = Profile(
            goal_label=ctx["athlete"]["goal"], goal_km=ctx["athlete"]["goal_km"], race_date=date.fromisoformat(ctx["athlete"]["race_date"]),
            experience=ctx["athlete"]["level"],
            availability={d: DayAvail(**ctx["availability"][d]) for d in DAYS}, long_run_day=ctx["long_run_day"])
        from coach.skeleton import Week
        weeks = [Week(date.fromisoformat(w["week_start"]), w["phase"], bool(w["recovery"]), bool(w["race_week"]), w["target_km"], w["long_run_km"])
                 for w in ctx["skeleton"]]
        done = next((w["already_done_km"] for w in ctx["skeleton"] if w["already_done_km"]), 0.0)
        return {"summary": "Here is your plan.", "workouts": reference_plan(p, weeks, date.fromisoformat(ctx["earliest_workout_date"]), done)}


def make_service(tmp_path, profile, coach=None, activities=None):
    s = Settings(db_path=str(tmp_path / "svc.db"), sync_interval_min=0)
    db = DB(s.db_path)
    uid = db.create_user("a@example.com", "A", "x")
    udb = db.for_user(uid)
    udb.save_profile(profile)
    if activities:
        udb.upsert_activities(activities)
    coach = coach or ReferenceCoach()
    strava = FakeStrava()
    strava.connected = bool(activities)
    svc = CoachService(udb, s, coach, strava, clock=lambda: TODAY)
    return svc, udb, coach


def weekly_totals(udb):
    totals = {}
    for w in udb.get_workouts():
        totals[w["week_start"]] = totals.get(w["week_start"], 0.0) + (w["distance_km"] or 0)
    return totals


def run_activity(i, d: date, km=10.0, minutes=60):
    return {"id": i, "start_local": f"{d.isoformat()}T07:00:00", "date": d.isoformat(), "name": "Run", "distance_m": km * 1000,
            "moving_s": minutes * 60, "avg_hr": 150.0, "elevation_m": 10.0, "sport": "Run"}


def test_the_reported_case_5k_regular_runner_with_default_baseline(tmp_path):
    """Intermediate, 'current weekly distance' left at 0 (the old form default), 8 weeks to a 5K."""
    p = prof(goal_km=5.0, weeks=8, level="intermediate", baseline=0, days=("tue", "thu", "sat", "sun"), minutes=75, long_minutes=120)
    svc, udb, coach = make_service(tmp_path, p)
    summary = svc.generate_plan()
    totals = weekly_totals(udb)
    mid = [v for k, v in totals.items() if k not in (monday(TODAY).isoformat(), monday(p.race_date).isoformat())]
    assert max(mid) >= 30, f"peak week only {max(mid):.0f} km: {totals}"            # was ~15 before the research
    assert "Volume:" in summary and "assumed" in summary and "typical week for a intermediate runner" in summary
    outlook = udb.kv_get("volume_outlook")
    assert outlook["baseline_source"] == "assumed" and outlook["peak_km"] >= 30
    assert outlook["ideal_low"] == 30 and outlook["goal"] == "5K"


def test_the_plan_stays_inside_the_researched_rules_end_to_end(tmp_path):
    p = prof(goal_km=10.0, weeks=12, level="intermediate", baseline=30, days=("mon", "tue", "thu", "sat", "sun"), minutes=75, long_minutes=120)
    svc, udb, _ = make_service(tmp_path, p)
    svc.generate_plan()
    workouts = udb.get_workouts()
    by_week = {}
    for w in workouts:
        by_week.setdefault(w["week_start"], []).append(w)
    weeks = {w["week_start"]: w for w in udb.get_weeks()}
    for ws, items in by_week.items():
        wk = weeks[ws]
        qual = [date.fromisoformat(w["date"]) for w in items if w["type"] in K.QUALITY_TYPES]
        longs = [date.fromisoformat(w["date"]) for w in items if w["type"] == "long"]
        assert len(qual) <= K.quality_limit("intermediate", wk["phase"], bool(wk["recovery"]), 5), ws
        assert all(b - a >= timedelta(days=2) for a, b in zip(sorted(qual), sorted(qual)[1:])), ws
        assert all(abs((q - l).days) >= 2 for q in qual for l in longs), ws
        km = sum(w["distance_km"] for w in items)
        if not wk["race_week"] and ws != monday(TODAY).isoformat():
            assert wk["target_km"] * 0.85 <= km <= wk["target_km"] * 1.10 + 0.5, (ws, km, wk["target_km"])
            hard = sum(w["distance_km"] for w in items if w["type"] in K.QUALITY_TYPES)
            assert hard <= K.HARD_SHARE_MAX * km + 0.1, ws                                  # at most 45% of the week's distance is in hard sessions
        for w in items:
            if w["type"] != "race":                                                          # a race may fall on any weekday
                assert DAYS[date.fromisoformat(w["date"]).weekday()] in ("mon", "tue", "thu", "sat", "sun")
    assert workouts[-1]["type"] == "race" and workouts[-1]["date"] == p.race_date.isoformat()


def test_beginner_starting_from_zero_is_conservative_and_told_why_it_is_limited(tmp_path):
    p = prof(goal_km=5.0, weeks=10, level="beginner", baseline=0, days=("tue", "thu", "sun"), minutes=30, long_minutes=40)
    svc, udb, _ = make_service(tmp_path, p)
    summary = svc.generate_plan()
    o = udb.kv_get("volume_outlook")
    assert o["baseline_source"] == "stated" and o["start_km"] <= 10                        # a true "just starting"
    lo, _, hi = K.peak_range(5.0, "beginner")
    assert o["peak_km"] <= hi and o["peak_km"] >= lo - 0.5
    assert o["limit"] == "time" and "available running time" in summary                    # 3 short runs cap the volume, and it says so
    totals = weekly_totals(udb)
    assert max(totals.values()) <= hi + 1


def test_strava_history_overrides_what_was_typed(tmp_path):
    runs = []
    base = monday(TODAY) - timedelta(weeks=4)
    for w in range(4):
        for k, day in enumerate((0, 2, 4, 6)):
            runs.append(run_activity(1000 + w * 10 + k, base + timedelta(weeks=w, days=day), km=10, minutes=52))
    p = prof(goal_km=21.0975, weeks=14, level="advanced", baseline=10, days=DAYS, minutes=90, long_minutes=150)
    svc, udb, coach = make_service(tmp_path, p, activities=runs)
    svc.generate_plan()
    o = udb.kv_get("volume_outlook")
    assert o["baseline_source"] == "strava" and o["baseline_km"] == pytest.approx(40, abs=0.5)
    assert o["start_km"] >= 39.5                                                            # not the 10 km that was typed
    ctx = coach.calls[0][1]
    assert ctx["recent_training"]["have_strava_history"] and ctx["volume_plan"]["baseline_source"] == "strava"


def test_a_layoff_restarts_below_the_stated_volume(tmp_path):
    old = [run_activity(1, TODAY - timedelta(days=25), km=10)]
    p = prof(goal_km=10.0, weeks=12, level="intermediate", baseline=30, minutes=75)
    svc, udb, coach = make_service(tmp_path, p, activities=old)
    svc.generate_plan()
    o = udb.kv_get("volume_outlook")
    assert o["baseline_source"] == "restart" and o["baseline_km"] == pytest.approx(30 * K.restart_fraction(25), abs=0.1)
    assert coach.calls[0][1]["layoff"]["suggested_restart_share_of_previous_volume"] == 0.60


def test_review_after_a_pain_report_keeps_the_next_week_easy(tmp_path):
    p = prof(goal_km=10.0, weeks=12, level="intermediate", baseline=30, days=("mon", "tue", "thu", "sat", "sun"), minutes=75, long_minutes=120)
    svc, udb, coach = make_service(tmp_path, p)
    svc.generate_plan()
    assert any(w["type"] in K.QUALITY_TYPES and w["date"] <= (TODAY + timedelta(days=7)).isoformat() for w in udb.get_workouts()), \
        "precondition: the normal plan has hard sessions in the next 7 days"
    svc.review("my left calf is really tight and sore")
    near = [w for w in udb.get_workouts(TODAY, TODAY + timedelta(days=6)) if w["status"] == "planned"]
    assert near and all(w["type"] not in K.QUALITY_TYPES for w in near)
    assert coach.calls[-1][1]["injury_or_pain_mentioned_in_check_in"] is True
    assert coach.calls[0][1]["injury_or_pain_mentioned_in_check_in"] is False


def test_goal_time_gives_code_computed_paces_to_the_coach_and_the_athlete(tmp_path):
    p = prof(goal_km=5.0, weeks=10, level="intermediate", baseline=25, goal_time_s=20 * 60)
    svc, udb, coach = make_service(tmp_path, p)
    svc.generate_plan()
    ctx = coach.calls[0][1]
    expected = K.pace_text(K.training_paces(K.vdot(5000, 1200)))
    assert ctx["training_paces_per_km"] == expected and "VDOT" in ctx["training_paces_basis"]
    assert set(ctx["workout_menu_by_phase"]) >= {"base", "build", "peak"} and "taper" in ctx["workout_menu_by_phase"]
    assert all("max_quality_sessions" in w for w in ctx["skeleton"])
    assert ctx["recommended_runs_per_week_for_this_goal_and_level"] == 4
    assert udb.kv_get("volume_outlook")["paces_s"]["threshold"][0] == pytest.approx(K.training_paces(K.vdot(5000, 1200))["threshold"][0])


def test_no_goal_time_and_no_runs_means_effort_only(tmp_path):
    svc, udb, coach = make_service(tmp_path, prof(baseline=20))
    svc.generate_plan()
    ctx = coach.calls[0][1]
    assert ctx["training_paces_per_km"] is None and "effort" in ctx["training_paces_basis"]


def test_too_little_time_to_the_race_is_called_a_compressed_build(tmp_path):
    p = prof(goal_km=21.0975, weeks=4, level="beginner", baseline=12, days=("tue", "thu", "sat", "sun"), minutes=60, long_minutes=120)
    svc, udb, _ = make_service(tmp_path, p)
    assert "compressed" in svc.generate_plan()


def test_review_follows_actual_training_not_the_typed_number(tmp_path):
    # Typed 40 km/week but the last four weeks were 12 km/week: a review must ramp from what really happened.
    runs = []
    base = monday(TODAY) - timedelta(weeks=4)
    for w in range(4):
        runs.append(run_activity(2000 + w, base + timedelta(weeks=w, days=1), km=6, minutes=40))
        runs.append(run_activity(2100 + w, base + timedelta(weeks=w, days=5), km=6, minutes=40))
    p = prof(goal_km=10.0, weeks=12, level="intermediate", baseline=40, minutes=90)
    svc, udb, coach = make_service(tmp_path, p, activities=runs)
    svc.generate_plan()
    svc.review("")
    o = udb.kv_get("volume_outlook")
    assert o["baseline_source"] == "strava" and o["baseline_km"] == pytest.approx(12, abs=0.5) and o["start_km"] <= 14


# ----------------------------------------------- the web layer ------------------------------------------------
SETUP = {"goal_preset": "5K", "race_date": str(TODAY + timedelta(weeks=8)), "units": "km", "experience": "intermediate", "long_run_day": "sun",
         "av_tue": "on", "min_tue": "60", "av_thu": "on", "min_thu": "60", "av_sat": "on", "min_sat": "60", "av_sun": "on", "min_sun": "120"}


def web_client(tmp_path, coach=None):
    s = Settings(db_path=str(tmp_path / "web.db"), sync_interval_min=0)
    app = create_app(s, db=DB(s.db_path), coach=coach or ReferenceCoach(), strava_factory=lambda u: FakeStrava(), clock=lambda: TODAY)
    c = TestClient(app)
    signup(c)
    return c


def test_setup_form_requires_the_weekly_distance_and_explains_levels(tmp_path):
    c = web_client(tmp_path)
    page = c.get("/setup").text
    assert 'name="baseline"' in page and "required" in page and "Be honest" in page
    assert "New to running, returning after a long break" in page                          # the level definitions are shown
    r = c.post("/setup", data=SETUP, follow_redirects=True)                                # baseline omitted
    assert "tell us roughly how much you run" in r.text
    assert c.get("/", follow_redirects=False).headers["location"] == "/setup"             # nothing was saved
    assert c.post("/setup", data={**SETUP, "baseline": "-5"}, follow_redirects=True).text.count("can&#39;t be negative") == 1
    assert c.post("/setup", data={**SETUP, "baseline": "0"}, follow_redirects=False).status_code == 303   # 0 is allowed: just starting


def test_plan_page_shows_volume_numbers_and_paces(tmp_path):
    c = web_client(tmp_path)
    c.post("/setup", data={**SETUP, "baseline": "25", "goal_time": "20:00"})
    c.post("/plan/generate")
    assert wait_job(c)["status"] == "done"
    page = c.get("/plan").text
    assert "Your training numbers" in page and "start → peak" in page and "Published intermediate 5K plans peak" in page
    thr = K.training_paces(K.vdot(5000, 20 * 60))["threshold"][0]
    assert "Training paces" in page and "Threshold" in page
    assert K.fmt_pace(thr) in page                                   # the exact pace computed in code, e.g. 4:15/km
    assert "km per week" in page


def test_paces_convert_for_mile_users(tmp_path):
    c = web_client(tmp_path)
    c.post("/setup", data={**SETUP, "baseline": "15", "units": "mi", "goal_time": "20:00"})
    c.post("/plan/generate")
    assert wait_job(c)["status"] == "done"
    page = c.get("/plan").text
    thr_mi = K.training_paces(K.vdot(5000, 20 * 60))["threshold"][0] * K.KM_PER_MI
    assert f"{int(thr_mi // 60)}:{int(thr_mi % 60):02d}/mi" in page   # threshold per mile, not per km
    assert "mi per week" in page and "km per week" not in page
