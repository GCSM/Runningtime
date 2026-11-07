"""Training paces follow what the athlete can run now and build toward the goal (they are not simply the goal's paces)."""
from datetime import date, timedelta

import pytest

from coach import knowledge as K
from coach.knowledge import VDOT_GAIN_PER_WEEK, VDOT_MAX_BELOW_GOAL, working_vdot

GOAL = 50.0


def test_no_evidence_means_the_goal():
    assert working_vdot(GOAL, None) == GOAL


def test_an_athlete_better_than_the_goal_is_trained_at_their_own_fitness():
    assert working_vdot(GOAL, 53.0, 10) == 53.0


def test_fitness_grows_by_a_fixed_rate_toward_the_goal_and_never_past_it():
    assert working_vdot(GOAL, 48.0, 0) == 48.0
    assert working_vdot(GOAL, 48.0, 5) == pytest.approx(48.0 + 5 * VDOT_GAIN_PER_WEEK)
    assert working_vdot(GOAL, 48.0, 50) == GOAL


def test_easy_run_evidence_cannot_drag_paces_far_below_the_goal():
    assert working_vdot(GOAL, 30.0, 0) == GOAL - VDOT_MAX_BELOW_GOAL
    assert working_vdot(GOAL, 30.0, 100) == GOAL


def test_paces_are_monotone_in_vdot():
    slow, fast = K.training_paces(45.0), K.training_paces(50.0)
    assert all(fast[k][0] < slow[k][0] for k in slow)


def test_the_service_uses_recent_evidence_and_says_so(tmp_path):
    from test_scenarios import TODAY, make_service, prof, run_activity
    p = prof(goal_km=10.0, weeks=16, baseline=30.0, goal_time_s=45 * 60)         # goal VDOT ~ 45
    race = [run_activity(1, TODAY - timedelta(days=10), km=5.0, minutes=24.0)]   # a 5K in 24:00 (VDOT ~ 41.5)
    svc, udb, _ = make_service(tmp_path, p, activities=race)
    paces, basis, _ = svc._paces(p, race)
    goal_paces = K.training_paces(K.vdot(10000, 45 * 60))
    assert "building toward your goal" in basis
    assert paces["threshold"][0] > goal_paces["threshold"][0]                       # slower than the goal's threshold pace: it is where they are now
    no_runs = svc._paces(p, [])
    assert no_runs[1].startswith("goal time") and no_runs[0] == goal_paces


def test_no_goal_time_still_uses_a_real_effort_for_paces(tmp_path):
    from test_scenarios import TODAY, make_service, prof, run_activity
    p = prof(goal_km=10.0, weeks=16, baseline=30.0, goal_time_s=None)
    runs = [run_activity(1, TODAY - timedelta(days=10), km=5.0, minutes=24.0)]
    svc, udb, _ = make_service(tmp_path, p, activities=runs)
    paces, basis, pace = svc._paces(p, runs)
    assert paces and "VDOT" in basis
