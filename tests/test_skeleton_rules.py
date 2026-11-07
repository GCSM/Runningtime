"""Exact tests of the weekly skeleton (skeleton.build_skeleton_info): phases, the build ladder, mid-plan rebuilds, taper, race week, and the
reason a plan is limited. Added after mutation testing showed ~30% of deliberate bugs here went unnoticed (notably the mid-plan rebuild and the
'limit' classification, which is what the athlete is told about their plan)."""
from datetime import date, timedelta

import pytest

from coach import knowledge as K
from coach.skeleton import build_skeleton_info, monday

T = date(2026, 11, 9)                    # Monday


def sk(goal=10.0, level="intermediate", weeks=16, base=30.0, days=4, mins=900, long_min=240, pace=390.0, today=None, start=None, recent_long=0.0, race_days=-1, other=None):
    start = start or T
    today = today or start
    race = start + timedelta(weeks=weeks, days=race_days)
    return build_skeleton_info(plan_start=start, today=today, race_date=race, goal_km=goal, baseline_km=base, experience=level, weekly_minutes=mins,
                               long_day_minutes=long_min, easy_pace_s_km=pace, run_days=days, longest_recent_km=recent_long, other_day_minutes=other)


def phases(weeks):
    return [w.phase for w in weeks]


# --------------------------------------------------------------------------------------------------- structure ----
def test_phases_split_the_training_weeks_40_40_20_and_end_with_taper_and_race():
    w, _ = sk(goal=21.0975, weeks=24)                       # half marathon: 2 taper weeks incl. race week -> 1 taper + race; 22 training weeks
    train = [x for x in w if x.phase in ("base", "build", "peak")]
    assert len(train) == 22 and phases(w)[-2:] == ["taper", "race"]
    assert [x.phase for x in train].count("base") == 9 and [x.phase for x in train].count("build") == 9 and [x.phase for x in train].count("peak") == 4
    order = [{"base": 0, "build": 1, "peak": 2, "taper": 3, "race": 4}[p] for p in phases(w)]
    assert order == sorted(order)
    assert w[-1].race_week and not any(x.race_week for x in w[:-1])
    assert w[0].week_start == T and all(b.week_start - a.week_start == timedelta(weeks=1) for a, b in zip(w, w[1:]))


def test_taper_length_follows_the_distance_and_is_truncated_for_very_short_plans():
    for goal, expected in ((5.0, ["race"]), (10.0, ["race"]), (21.0975, ["taper", "race"]), (42.195, ["taper", "taper", "race"])):
        w, _ = sk(goal=goal, weeks=20)
        assert phases(w)[-len(expected):] == expected
        assert all(p in ("base", "build", "peak") for p in phases(w)[:-len(expected)])
    w, _ = sk(goal=42.195, weeks=3)                          # three calendar weeks, three taper weeks' worth: all taper, no training weeks
    assert phases(w) == ["taper", "taper", "race"] and len(w) == 3
    w, _ = sk(goal=42.195, weeks=2)                          # two weeks: only the last two entries of the taper survive
    assert phases(w) == ["taper", "race"]
    w, _ = sk(goal=42.195, weeks=1)
    assert phases(w) == ["race"]


def test_a_step_back_week_comes_every_fourth_week_counted_back_from_the_peak_block():
    w, _ = sk(goal=42.195, weeks=24, days=5, base=40)
    n_train = len([x for x in w if x.phase in ("base", "build", "peak")])
    rec = [i for i, x in enumerate(w) if x.recovery]
    assert rec == [i for i in range(n_train) if i >= 2 and (n_train - 3 - i) % 4 == 0 and i <= n_train - 3]
    assert n_train - 3 in rec and (n_train - 1) not in rec and (n_train - 2) not in rec        # the last two training weeks are the peak block
    short, _ = sk(goal=5.0, weeks=5)                         # 4 training weeks: too short for step-backs
    assert not any(x.recovery for x in short)


def test_step_back_weeks_are_a_quarter_lighter_in_volume_and_long_run():
    w, _ = sk(goal=42.195, weeks=24, days=5, base=40)
    for i, x in enumerate(w):
        if x.recovery:
            prev_build = [y for y in w[:i] if not y.recovery][-1]
            assert x.target_km == pytest.approx(prev_build.target_km * 0.75, abs=0.75) or x.target_km <= 0.8 * prev_build.target_km
            assert x.long_run_km <= 0.75 * max(y.long_run_km for y in w[max(0, i - 4):i]) + 0.01


def test_race_week_is_the_race_plus_easy_running_and_the_taper_long_runs_step_down():
    w, o = sk(goal=42.195, weeks=24, days=5, base=40, mins=600)
    peak = max(x.target_km for x in w if x.phase in ("base", "build", "peak"))
    assert w[-1].target_km == pytest.approx(K.race_week_km(42.195, peak), abs=0.75) and w[-1].long_run_km == 0
    peak_long = max(x.long_run_km for x in w if x.phase in ("base", "build", "peak"))
    t1, t2 = [x for x in w if x.phase == "taper"]
    assert t1.long_run_km == pytest.approx(0.62 * peak_long, abs=0.6) and t2.long_run_km == pytest.approx(0.42 * peak_long, abs=0.6)
    assert t1.target_km == pytest.approx(0.8 * peak, abs=0.75) and t2.target_km == pytest.approx(0.65 * peak, abs=0.75)
    h, _ = sk(goal=21.0975, weeks=14, days=5, base=30, mins=600)
    assert [x.long_run_km for x in h if x.phase == "taper"] == [pytest.approx(0.65 * max(x.long_run_km for x in h if x.phase in ("base", "build", "peak")), abs=0.6)]


@pytest.mark.parametrize("weekday,shifted", [(0, True), (1, True), (2, True), (3, False), (4, False), (5, False), (6, False)])
def test_a_race_on_monday_to_wednesday_gets_its_taper_in_the_week_before(weekday, shifted):
    w, _ = sk(goal=10.0, weeks=12, race_days=weekday)
    assert w[-1].race_week and w[-1].week_start == monday(T + timedelta(weeks=12, days=weekday))
    if shifted:
        assert w[-2].phase == "taper" and w[-2].long_run_km == 0 and w[-1].target_km == pytest.approx(14.0, abs=0.01)
        assert w[-2].target_km == pytest.approx(K.race_week_km(10.0, max(x.target_km for x in w[:-2])) - 10.0, abs=0.75)
    else:
        assert w[-1].target_km > 14.5 and w[-2].phase in ("peak", "build", "base")


# ------------------------------------------------------------------------------------------------ the build ladder --
def test_volume_climbs_evenly_to_the_peak_and_holds_it_for_two_build_weeks():
    w, o = sk(goal=21.0975, weeks=20, base=30, days=5, mins=900)
    build = [x for x in w if x.phase in ("base", "build", "peak") and not x.recovery]
    vals = [x.target_km for x in build]
    assert vals == sorted(vals) and vals[0] == 30.0 and vals[-1] == vals[-2] == o.peak_km     # two weeks at peak
    assert vals[-3] < vals[-2]
    steps = [b - a for a, b in zip(vals, vals[1:-2])]
    assert max(steps) - min(steps) <= 1.0 + 1e-9                                               # an even climb (rounding to half-km aside)
    few, _ = sk(goal=5.0, weeks=7, base=10, level="beginner", days=3)                        # <6 build weeks: a single peak week
    fv = [x.target_km for x in few if x.phase in ("base", "build", "peak") and not x.recovery]
    assert len(fv) <= 5 and fv[-1] == max(fv) and (len(fv) < 2 or fv[-2] < fv[-1] or fv[-2] == fv[-1])


def test_the_first_long_run_is_forty_percent_of_the_week_and_grows_by_the_rule():
    w, _ = sk(goal=42.195, weeks=24, base=40, days=5, mins=900)
    assert w[0].long_run_km == 16.0 and w[0].target_km == 40.0
    longs = [x.long_run_km for x in w if x.phase in ("base", "build", "peak")]
    for i, k in enumerate(longs):
        prior = max(longs[max(0, i - 4):i], default=0)
        if prior:
            assert k <= max(prior * 1.1, prior + 2.5) + 0.5


# ----------------------------------------------------------------------------------------- rebuilding mid-plan -----
def test_a_review_in_the_middle_of_the_plan_keeps_the_same_step_back_weeks():
    full, _ = sk(goal=42.195, weeks=24, days=5, base=40)
    for k in (3, 7, 10, 14, 18):
        mid, _ = sk(goal=42.195, weeks=24, days=5, base=full[k].target_km, start=T, today=T + timedelta(weeks=k))
        assert [x.recovery for x in mid] == [x.recovery for x in full[k:]] and [x.phase for x in mid] == [x.phase for x in full[k:]]


def test_a_mid_plan_rebuild_steps_up_from_what_was_achieved_not_from_zero_and_not_flat():
    # eight weeks in, the athlete has been averaging 50 km with a 22 km longest run recently
    mid, o = sk(goal=42.195, weeks=24, days=5, base=60.0, start=T, today=T + timedelta(weeks=8), recent_long=22.0, mins=900)
    first = mid[0]
    assert first.target_km > 60.0                                                   # this week is one step up from the baseline (progress each Monday)
    assert first.target_km <= 60.0 + K.max_weekly_increase_km(60.0, "intermediate") + 0.5
    assert first.long_run_km > 22.0 and first.long_run_km <= max(22 * 1.1, 24.5) + 0.01   # one step up the ladder from the recent longest run
    nxt = [x for x in mid if not x.recovery][1]
    assert nxt.target_km >= first.target_km and mid[0].week_start == T + timedelta(weeks=8)


def test_a_mid_plan_rebuild_is_not_lower_than_a_fresh_rebuild_with_the_same_numbers_would_allow():
    no_hist, _ = sk(goal=42.195, weeks=24, days=5, base=50.0, start=T, today=T + timedelta(weeks=8), recent_long=0.0, mins=900)
    assert no_hist[0].long_run_km <= 0.40 * no_hist[0].target_km + 0.5                    # with no known long run the 40% rule applies
    with_hist, _ = sk(goal=42.195, weeks=24, days=5, base=50.0, start=T, today=T + timedelta(weeks=8), recent_long=18.0, mins=900)
    assert with_hist[0].long_run_km > 18.0


# ------------------------------------------------------------------------------------ why a plan is limited --------
@pytest.mark.parametrize("kw,limit", [
    (dict(goal=10.0, level="intermediate", weeks=20, base=60.0, days=6, mins=900), "none"),            # already above the published target
    (dict(goal=10.0, level="intermediate", weeks=20, base=25.0, days=5, mins=900, long_min=150), "goal"),
    (dict(goal=10.0, level="intermediate", weeks=20, base=25.0, days=4, mins=240, long_min=90), "time"),
    (dict(goal=42.195, level="intermediate", weeks=24, base=35.0, days=4, mins=900, long_min=300), "frequency"),
    (dict(goal=42.195, level="beginner", weeks=12, base=10.0, days=5, mins=900, long_min=300), "ramp"),
])
def test_the_limit_reason_matches_what_actually_stops_the_build(kw, limit):
    w, o = sk(**kw)
    assert o.limit == limit
    peak = max(x.target_km for x in w if x.phase in ("base", "build", "peak"))
    low, target, high = K.peak_range(kw["goal"], kw["level"])
    if limit == "goal":
        assert abs(peak - target) <= 1.0
    if limit == "time":
        assert peak <= o.time_cap_km + 0.75 < target
    if limit == "frequency":
        assert peak <= o.freq_cap_km + 0.75 < target
    if limit == "ramp":
        assert peak < min(target, o.time_cap_km, o.freq_cap_km) - 0.25
    text = o.athlete_text("goal", kw["level"])
    assert {"time": "available running time", "frequency": "runs a week", "ramp": "aren't enough weeks"}.get(limit, "Volume:") in text


def test_weekly_volume_never_exceeds_what_the_days_can_carry_even_for_an_athlete_who_says_they_run_more():
    w, o = sk(goal=10.0, level="beginner", weeks=14, base=100.0, days=4, mins=200, long_min=60)
    cap = min(o.time_cap_km, o.freq_cap_km)
    assert max(x.target_km for x in w if x.phase in ("base", "build", "peak")) <= max(cap, K.MIN_BASE_KM) + 0.75
    w2, _ = sk(goal=5.0, level="beginner", weeks=10, base=100.0, days=6, mins=900, long_min=240)
    assert max(x.target_km for x in w2 if x.phase in ("base", "build", "peak")) <= K.peak_range(5.0, "beginner")[2] * 1.25 + 0.75     # not 100 km for a 5K


def test_capacity_counts_what_each_day_can_hold_not_just_total_minutes():
    # 4 weekday runs of 75 minutes and a 5-hour long-run day: the long-run day can't carry more than the long-run caps allow
    cap = K.run_capacity_km("intermediate", 42.195, [75, 75, 75, 75], 300, 390.0)
    assert cap == pytest.approx(4 * 75 * 60 / 390 + 32.0, abs=0.2)             # the long run is capped at the 32 km event cap, not 300 min / 6.5 = 46 km
    cap_short = K.run_capacity_km("intermediate", 10.0, [60, 60, 60], 90, 390.0)
    assert cap_short == pytest.approx(3 * 60 * 60 / 390 + 90 * 60 / 390, abs=0.2)
    assert K.run_capacity_km("intermediate", 10.0, [0, 0], 0, 390.0) > 0                          # unspecified minutes fall back to the caps
