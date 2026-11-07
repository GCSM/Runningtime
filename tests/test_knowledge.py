"""coach/knowledge.py pinned to docs/RESEARCH.md: if a constant drifts away from the research, a test here fails."""
import math

import pytest

from coach import knowledge as K


def hms(s):
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{int(round(s % 60)):02d}"


# ---- pace maths (RESEARCH.md §9) -----------------------------------------------------------------------------------
@pytest.mark.parametrize("dist_m,ref_s,tol", [(5000, 19 * 60 + 57, 6), (10000, 41 * 60 + 21, 10), (21097.5, 3600 + 31 * 60 + 35, 15), (42195, 3 * 3600 + 10 * 60 + 49, 30)])
def test_vdot_50_matches_published_daniels_equivalents(dist_m, ref_s, tol):
    # Published Daniels table values for VDOT 50 (checked against several calculators); our equations must land within seconds.
    assert abs(K.race_time_s(50, dist_m) - ref_s) <= tol


def test_vdot_roundtrip_and_known_values():
    for v in (30, 40, 50, 60, 70):
        for d in (5000, 10000, 21097.5, 42195):
            assert K.vdot(d, K.race_time_s(v, d)) == pytest.approx(v, abs=0.01)
    assert K.vdot(5000, 20 * 60) == pytest.approx(49.8, abs=0.2)


def test_riegel_20_minute_5k_to_half_is_about_1_32():
    assert K.riegel(20 * 60, 5, 21.0975) == pytest.approx(92 * 60, abs=10)
    assert K.riegel(20 * 60, 5, 5) == pytest.approx(20 * 60)
    assert K.riegel(20 * 60, 5, 10, exponent=1.04) < K.riegel(20 * 60, 5, 10, exponent=1.08)


def test_training_paces_are_ordered_and_plausible():
    paces = K.training_paces(50)
    order = [paces["repetition"][0], paces["interval"][0], paces["threshold"][0], paces["marathon"][0], paces["easy"][0], paces["easy"][1]]
    assert order == sorted(order) and len(set(order)) == len(order)         # faster zone = smaller s/km, strictly
    assert 250 <= paces["threshold"][0] <= 262                              # VDOT 50 threshold ≈ 4:15/km
    assert 232 <= paces["interval"][0] <= 240                               # ≈ 3:54/km
    assert paces["easy"][1] - paces["easy"][0] > 30                         # easy is a range, not a point
    assert paces["threshold"][0] == paces["threshold"][1]
    faster = K.training_paces(60)
    assert all(faster[z][0] < paces[z][0] for z in paces)                   # fitter runner = faster paces


def test_paces_from_easy_follow_claude_coach_offsets():
    p = K.paces_from_easy(390)                                              # 6:30/km easy
    t = 390 - 60
    assert p["threshold"][0] == t and p["marathon"][0] == t + 20 and p["interval"][0] == t - 17 and p["repetition"][0] == t - 30


def test_pace_formatting():
    assert K.fmt_pace(255) == "4:15/km" and K.fmt_pace(299.6) == "5:00/km"
    text = K.pace_text(K.training_paces(50))
    assert "–" in text["easy"] and "–" not in text["threshold"]


# ---- volumes (RESEARCH.md §2) ---------------------------------------------------------------------------------------
def test_peak_ranges_are_well_formed_and_ordered():
    for lv in K.LEVELS:
        for g, (lo, tgt, hi) in K.PEAK_KM[lv].items():
            assert 0 < lo < tgt < hi, (lv, g)
    for g in K.ANCHORS:
        targets = [K.PEAK_KM[lv][g][1] for lv in K.LEVELS]
        assert targets == sorted(targets), g                                 # more experienced => more volume
    for lv in K.LEVELS:
        targets = [K.PEAK_KM[lv][g][1] for g in K.ANCHORS]
        assert targets == sorted(targets), lv                                # longer race => more volume


@pytest.mark.parametrize("goal,level,lo_bound,hi_bound", [
    (5, "beginner", 12, 32), (5, "intermediate", 29, 48), (5, "advanced", 48, 110),
    (10, "intermediate", 40, 64), (21.0975, "beginner", 30, 64), (21.0975, "intermediate", 45, 80), (42.195, "advanced", 80, 140),
])
def test_targets_sit_inside_the_published_ranges(goal, level, lo_bound, hi_bound):
    lo, tgt, hi = K.peak_range(goal, level)
    assert lo_bound <= tgt <= hi_bound


def test_custom_distances_interpolate_and_clamp():
    lo10, t10, _ = K.peak_range(10, "intermediate")
    lo21, t21, _ = K.peak_range(21.0975, "intermediate")
    assert t10 < K.peak_range(15, "intermediate")[1] < t21
    assert K.peak_range(3, "beginner") == K.peak_range(5, "beginner")
    assert K.peak_range(60, "advanced") == K.peak_range(42.195, "advanced")


def test_goal_names_and_assumed_baselines():
    assert [K.goal_name(x) for x in (5, 8, 10, 21.0975, 30, 42.195)] == ["5K", "10K", "10K", "half marathon", "marathon", "marathon"]
    assert K.assumed_baseline_km("beginner") < K.assumed_baseline_km("intermediate") < K.assumed_baseline_km("advanced")
    assert K.assumed_baseline_km("nonsense") == K.assumed_baseline_km("intermediate")


# ---- progression (RESEARCH.md §3) -----------------------------------------------------------------------------------
@pytest.mark.parametrize("level", K.LEVELS)
def test_ramp_is_percentage_with_a_small_floor(level):
    assert K.next_week_km(100, level) == pytest.approx(100 * (1 + K.RAMP_PCT[level]))     # high volume: pure percentage
    low = K.next_week_km(10, level) - 10
    assert low == K.RAMP_FLOOR_KM[level] and low / 10 <= 0.26                             # low volume: floor, never > ~25%
    assert 0.05 <= K.RAMP_PCT[level] <= 0.12


def test_recovery_cadence_and_cut():
    assert K.RECOVERY_EVERY in (3, 4) and 0.6 <= K.RECOVERY_FACTOR <= 0.8


# ---- long runs (RESEARCH.md §4) -------------------------------------------------------------------------------------
def test_long_run_caps_follow_claude_coach_event_targets():
    assert K.long_run_cap_km(5, "advanced") <= 12 and K.long_run_cap_km(10, "advanced") <= 16
    assert K.long_run_cap_km(21.0975, "advanced") <= 22 and K.long_run_cap_km(42.195, "advanced") <= 35
    for g in K.ANCHORS:
        caps = [K.long_run_cap_km(g, lv) for lv in K.LEVELS]
        assert caps == sorted(caps)
    assert K.long_run_max_minutes(42.195) == 300 and K.long_run_max_minutes(10) == 150


def test_long_run_share_depends_on_distance_and_run_days():
    assert K.long_run_share(20, 3, 5) == 0.40          # Higdon Novice 5K: long run ≈ 37% with 3 runs
    assert K.long_run_share(40, 5, 10) == 0.30 and K.long_run_share(70, 5, 10) == 0.25
    assert K.long_run_share(60, 5, 21.0975) == 0.35 and K.long_run_share(70, 5, 42.195) == 0.42 and K.long_run_share(60, 4, 42.195) == 0.60   # 4-day marathon: the 32 km long run is most of the week


def test_long_run_km_is_bounded_by_every_limit():
    pace = 390
    assert K.long_run_km(37, 5, "intermediate", 4, 75, pace) == 11.0                           # event cap
    assert K.long_run_km(37, 5, "intermediate", 4, 30, pace) == 4.5                            # that day's minutes
    assert K.long_run_km(20, 5, "beginner", 3, 120, pace) == 8.0                               # 40% of 20 = 8 = beginner cap
    assert K.long_run_km(74, 42.195, "intermediate", 5, 300, pace) == 31.0                     # 42% of 74
    assert K.long_run_km(60, 42.195, "intermediate", 4, 300, pace) == 32.0                     # 4 days: 60% of 60 = 36, capped at 32
    assert K.long_run_km(120, 42.195, "advanced", 6, 400, pace) == 32.0                        # every marathon block peaks near 32 km


# ---- taper, plan length, hard work (RESEARCH.md §5–7) ---------------------------------------------------------------
def test_taper_length_grows_with_distance_and_volume_falls():
    lens = [len(K.taper_multipliers(g)) for g in (5, 10, 21.0975, 42.195)]
    assert lens == [1, 1, 2, 3]
    for g in (5, 10, 21.0975, 42.195):
        m = K.taper_multipliers(g)
        assert m == sorted(m, reverse=True) and 0.4 <= min(m) and max(m) <= 0.85       # 15–60% cut, progressive
    assert K.taper_multipliers(42.195) is not K.TAPER[42.195]                          # callers can't mutate the table


def test_minimum_weeks_fall_with_experience_and_rise_with_distance():
    for g in K.ANCHORS:
        w = [K.min_weeks(g, lv) for lv in K.LEVELS]
        assert w == sorted(w, reverse=True)
    assert K.min_weeks(42.195, "intermediate") >= 16                                   # claude-coach: marathon 16–20 weeks
    assert K.min_weeks(5, "beginner") >= 8


def test_quality_limits_by_level_phase_and_schedule():
    q = K.quality_limit
    assert q("beginner", "base", False, 3) == 0 and q("beginner", "build", False, 3) == 1
    assert q("intermediate", "base", False, 4) == 1 and q("intermediate", "build", False, 4) == 1      # 4 runs incl. the long run: one hard session
    assert q("intermediate", "build", False, 5) == 2 and q("advanced", "build", False, 4) == 2 and q("advanced", "build", False, 3) == 1
    assert q("intermediate", "build", False, 3) == 1                                   # needs 4+ run days for a second session
    assert q("advanced", "peak", False, 5) == 2
    assert q("advanced", "build", True, 5) == 0                                        # recovery week: no hard work
    assert q("advanced", "taper", False, 5) == 1 and q("beginner", "race", False, 3) == 1
    assert K.recommended_runs_per_week(5, "beginner") == 3 and K.recommended_runs_per_week(5, "intermediate") == 4
    assert K.recommended_runs_per_week(42.195, "advanced") == 6


def test_restart_fractions_decrease_with_time_off():
    f = [K.restart_fraction(d) for d in (2, 7, 20, 60, 200)]
    assert f == [1.0, 0.70, 0.60, 0.50, 0.30]


# ---- workout menu (RESEARCH.md §10) ---------------------------------------------------------------------------------
@pytest.mark.parametrize("goal", [5, 10, 21.0975, 42.195])
def test_workout_menu_covers_every_phase_and_has_easy_and_long(goal):
    menu = K.workout_menu(goal, "intermediate")
    assert set(menu) == {"base", "build", "peak", "taper", "race"}
    assert all(menu[ph] for ph in menu)
    assert any("Easy run" in m for m in menu["build"]) and any("Long run" in m for m in menu["base"])
    assert any("Shakeout" in m for m in menu["taper"])


def test_beginner_menu_offers_run_walk_and_5k_menu_has_5k_specific_work():
    assert any("Run/walk" in m for m in K.workout_menu(5, "beginner")["base"])
    assert not any("Run/walk" in m for m in K.workout_menu(5, "advanced")["base"])
    assert any("800 m" in m for m in K.workout_menu(5, "intermediate")["build"])
    assert any("goal 5K pace" in m for m in K.workout_menu(5, "intermediate")["peak"])


# ---- what the athlete is told ---------------------------------------------------------------------------------------
def _outlook(**kw):
    base = dict(baseline_km=22, baseline_source="strava", start_km=22, peak_km=37, ideal_low=30, ideal_target=38, ideal_high=46,
                limit="goal", time_cap_km=60, weeks_to_race=12, min_weeks=6)
    base.update(kw)
    return K.VolumeOutlook(**base)


def test_athlete_text_explains_source_and_limit():
    t = _outlook().athlete_text("5K", "intermediate")
    assert "22 km/week" in t and "37 km/week" in t and "30–46" in t and "Strava" in t
    assert "available running time" not in t and "enough weeks" not in t       # no limit sentence when the goal range is reachable
    assert "available running time" in _outlook(limit="time", time_cap_km=24).athlete_text("5K", "intermediate")
    assert "enough weeks" in _outlook(limit="ramp", weeks_to_race=6).athlete_text("5K", "intermediate")
    assert "assumed" in _outlook(baseline_source="assumed").athlete_text("5K", "intermediate")
    assert "compressed" in _outlook(weeks_to_race=4, min_weeks=6).athlete_text("5K", "intermediate")
    assert "compressed" not in _outlook(weeks_to_race=12).athlete_text("5K", "intermediate")
    assert math.isclose(_outlook().to_dict()["peak_km"], 37.0)


# ---- readiness: a marathon plan that can't prepare you must say so -----------------------------------------------
def _mk_outlook(goal_km=42.195, exp="intermediate", weeks=20, mins=240, long_min=90, days=4, base=25):
    from datetime import date, timedelta
    from coach.skeleton import build_skeleton_info
    t = date(2026, 11, 2)
    return build_skeleton_info(plan_start=t, today=t, race_date=t + timedelta(weeks=weeks), goal_km=goal_km, baseline_km=base, experience=exp,
                               weekly_minutes=mins, long_day_minutes=long_min, easy_pace_s_km=360, run_days=days)[1]


def test_short_time_marathon_is_flagged_with_the_fix():
    o = _mk_outlook()                                   # 4 days, 240 min/week, 90-minute long-run day: a 40 km peak and a 15 km long run
    assert o.peak_km < 42 and o.peak_long_km <= 15
    text = " ".join(o.warnings)
    assert "170+ minutes" in text and "310 minutes a week" in text and "won't fully prepare" in text
    assert "long-run day has 90 minutes" in text


def test_a_properly_scheduled_marathon_has_no_warnings():
    o = _mk_outlook(mins=420, long_min=210, days=5, weeks=24, base=30)
    assert o.peak_long_km >= 28 and o.peak_km >= 60 and o.warnings == []


def test_too_few_weeks_is_flagged_even_with_plenty_of_time():
    o = _mk_outlook(weeks=12, mins=400, long_min=200, base=20)
    assert o.warnings and "12 weeks" in " ".join(o.warnings) and "later race" in " ".join(o.warnings)


def test_shorter_goals_with_sensible_schedules_are_not_flagged():
    for goal, exp, mins, lm in [(5.0, "beginner", 150, 45), (10.0, "intermediate", 240, 75), (21.0975, "intermediate", 300, 120)]:
        assert _mk_outlook(goal, exp, weeks=14, mins=mins, long_min=lm, base=20).warnings == [], goal


def test_max_long_lets_a_slow_marathoner_reach_a_real_long_run_but_km_caps_still_bind():
    slow = 450.0                                                   # 7:30/km easy: a ~5 h marathon
    assert K.long_run_km(90, 42.195, "intermediate", 5, 300, slow) == 32.0          # open-ended: the km cap binds
    assert K.long_run_km(90, 42.195, "intermediate", 5, 210, slow) == 28.0
    assert K.long_run_km(110, 42.195, "advanced", 6, 240, 330.0) == 32.0            # fast runner: capped by distance, not time
    assert K.long_run_km(70, 21.0975, "intermediate", 5, 240, slow) <= 20.0         # half marathon cap unchanged


def test_a_six_hour_marathoner_can_still_cover_the_full_long_run():
    pace = 540.0                                                     # 9:00/km easy
    assert K.long_run_km(90, 42.195, "beginner", 5, 300, pace) == 32.0      # time allows 33 km; the 32 km marathon cap binds
    assert K.long_run_km(90, 42.195, "intermediate", 5, 300, pace) == 32.0  # 300 min = 33.3 km, capped at 32
    assert K.long_run_km(90, 42.195, "intermediate", 5, 240, pace) == 26.5  # a 240-minute day would have cost 5.5 km


# ---- run frequency and per-run size: a handful of runs must not carry a huge week -----------------------------------
def test_run_caps_by_level_type_and_goal():
    assert K.run_cap_minutes("intermediate", "easy", 5) == 70 and K.run_cap_minutes("intermediate", "tempo", 5) == 75
    assert K.run_cap_minutes("intermediate", "easy", 42.195) == 80 and K.run_cap_minutes("intermediate", "intervals", 42.195) == 75
    assert K.run_cap_minutes("beginner", "easy", 10) < K.run_cap_minutes("advanced", "easy", 10)


def test_frequency_cap_grows_with_run_days_and_marathon_peak_follows_it():
    caps = [K.frequency_cap_km("intermediate", 42.195, d, 390) for d in (3, 4, 5, 6)]
    assert caps == sorted(caps) and caps[0] < 50 < 70 < caps[2]
    for days, (lo, hi) in {4: (58, 68), 5: (74, 82)}.items():
        o = _mk_outlook(weeks=26, mins=500 * days // 4, long_min=240, days=days, base=35)
        assert lo <= o.peak_km <= hi, (days, o.peak_km)
    four = _mk_outlook(weeks=26, mins=500, long_min=240, days=4, base=35)
    assert four.limit == "frequency" and "Adding another run day" in four.athlete_text("marathon", "intermediate")


def test_the_reported_plan_shape_is_gone_no_78_km_on_four_runs():
    o = _mk_outlook(weeks=26, mins=420, long_min=240, days=4, base=35)
    assert o.peak_km <= 66                                                              # was 78 km on 4 runs: ~19.5 km per run, 3 of 4 runs hard


def test_every_marathon_block_peaks_its_long_run_near_32_km():
    for level, days, base in [("intermediate", 4, 35), ("intermediate", 5, 35), ("advanced", 5, 40), ("advanced", 6, 40)]:
        o = _mk_outlook(weeks=26, mins=900, long_min=300, days=days, base=base, exp=level)
        assert 31 <= o.peak_long_km <= 32, (level, days, o.peak_long_km)
    assert max(K.LONG_RUN_CAP_KM[lv][42.195] for lv in K.LEVELS) == min(K.LONG_RUN_CAP_KM[lv][42.195] for lv in K.LEVELS) == 32
    # the skeleton's long runs top out at 32 km and reach it more than once before the taper (growth limits are applied in plancheck)
    from datetime import date, timedelta
    from coach.skeleton import build_skeleton_info
    t = date(2026, 11, 2)
    weeks, _ = build_skeleton_info(plan_start=t, today=t, race_date=t + timedelta(weeks=26), goal_km=42.195, baseline_km=35, experience="intermediate",
                                   weekly_minutes=900, long_day_minutes=300, easy_pace_s_km=390, run_days=4)
    longs = [w.long_run_km for w in weeks if w.phase in ("base", "build", "peak")]
    assert max(longs) <= 32 and longs[-1] >= 28 and sum(1 for x in longs if x >= 31) >= 2   # reaches 32 more than once, then tapers
