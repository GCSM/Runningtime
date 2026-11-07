# Quality assurance for plan generation

A bad plan is a bad product, so plan generation gets the most testing. This document says what is tested, how, what the last deep QA pass found,
and what is still NOT covered.

## How a plan is made (what has to be right)

1. **Weekly skeleton** (`coach/skeleton.py`, rules in `coach/knowledge.py`): code decides each week's distance, long run, step-back weeks, taper
   and race week from the goal, level, schedule and what the athlete has actually run.
2. **The model** (`coach/llm.py`) turns the skeleton into day-by-day workouts: sessions, paces, descriptions.
3. **The guard** (`coach/plancheck.py`, `sanitize`) corrects anything the model gets wrong before the athlete sees it.
4. **Every Monday** the plan is rebuilt from what the athlete really ran (`service.review`), so errors can also compound *over time*.

## The test layers

| Layer | Files | What it catches |
|---|---|---|
| Rule-by-rule unit tests | `test_sanitize_rules.py`, `test_adherence_rules.py`, `test_skeleton_rules.py`, `test_knowledge.py` | one rule, exact numbers, both sides of every boundary |
| Properties over thousands of athletes | `test_scenarios.py` (2,700 skeletons), `test_plan_shape.py` (5,600+ configs) | shape: gradual build, late peak, step-backs, taper; hard limits |
| Hostile model answers | `test_fuzz_sanitize.py` (1,500 random malformed answers) | crashes, non-finite numbers, wrong days, stacked hard efforts |
| Plans over weeks | `test_athlete_sim.py` (9 simulated athletes, weekly reviews) | erosion, spirals, bad restarts after breaks, step-backs lost |
| Regression tests | `test_qa_regressions.py`, `test_plan_integrity.py` | each defect found in the QA below, by name |
| Executable definition of "good" | `coach/planaudit.py` | shape + rules, reused by tests and the live evaluation |
| Model in the loop | `tools/plan_eval.py` (`capture`, `live`, `audit`) | what the real prompt produces from a real model |
| Test-suite strength | `tools/mutation_test.py`, `coverage` | tests that pass whatever the code does |

`python -m pytest` runs everything except the live model. Line coverage of the plan modules is ~95-100%, which is why it is **not** the measure
that matters: the "32 km long run every week" defect existed at 99% coverage. Mutation testing (deliberately breaking the code and checking a test
fails) is: `python tools/mutation_test.py coach/plancheck.py 200`.

## What the deep QA pass found (all fixed, each has a test)

Found by an independent red-team review, an expert-coach review of model-written plans, fuzzing, athlete simulation and mutation testing.

| Defect | Found by | Why it mattered |
|---|---|---|
| Plan peaked early and held its peak (32 km long run in 11 of 24 weeks) | athlete report, then shape audit | no build-up, injury risk |
| Weekly reviews erased step-back weeks and re-started the ramp every Monday | athlete simulation | an athlete following the app never got a recovery week |
| After week 1 the baseline collapsed to a quarter (missing Strava weeks counted as zero) | athlete simulation | a 34 km plan became 12 km |
| Delivered volume shaved a few % off every Monday, so the plan lagged its own progression by 12% | athlete simulation | peak weeks never reached |
| After a long break the plan restarted at 8 km (or stayed low for weeks) | red team, simulation | wrong restart, slow rebuild; also a step-back week right after returning |
| First long run could be 60% of week 1 (21 km in a 35 km week) | live model test | injury risk |
| Race week was the race + 6 km; taper long runs were 90% of peak; races Mon-Wed had no taper | expert review, red team | under-/over-tapered |
| Weekly volume ignored run frequency and per-run size (78 km on 4 runs) | athlete report | huge runs, 3 of 4 hard |
| Planned volume could exceed what the athlete's days can carry (silent under-delivery) | probing | plan said 78, delivered 50 |
| `sanitize` crashed on NaN/inf/non-dict entries and divided by zero | fuzzing (46% of hostile answers) | a bad model answer = no plan |
| `sanitize` trusted distance/duration pairs (29 km in 3 minutes), wrong race distance, long runs on the wrong day, two long runs a week, hard sessions next to the long run across a Monday, a hard session the day before the race | red team, fuzzing | unsafe or nonsensical plans |
| A partial model answer deleted the rest of the plan (weeks showing 0 km) | athlete report | empty plan |
| Titles/descriptions described a different distance after scaling ("Tempo 12 km" saved as 8) | red team | confusing, wrong instructions |
| Units: miles athletes got km text, km paces, km warnings | red team | mixed units in the UI |
| Goal times were never validated (5K in 8 minutes) or sanity-checked against the athlete's runs | probing | absurd paces |
| Easy runs *slower* than the band lowered the execution score; "too fast" was counted for tempo sessions; suggestion with no reason | red team | wrong feedback |
| Restoring a skipped workout could double-book its day | red team | one run credited twice |
| A failed generation moved `plan_start` | red team | phases/step-backs computed from the wrong origin |
| A short morning run marked today "partial" and blocked the day; two runs in a day only counted the longest | red team | wrong adherence |
| Pain detector matched "painting", "tight schedule", "no pain" | red team | false pain mode |
| Background review retried a failing model every 30 minutes and ran inside the last 3 days | red team | wasted calls, errors |
| Rules jumped at preset distances (22.0 km half rules, 22.1 km marathon rules) | red team | cliffs |
| The goal-time easy pace overrode the athlete's real (slower) easy pace in every time budget | red team | impossible distances |

## Mutation-testing scores (share of deliberate bugs the tests notice)

| Module | Before | After |
|---|---|---|
| `coach/plancheck.py` (sanitizer) | 40% | see "Final numbers" below |
| `coach/skeleton.py` | 72% | see below |
| `coach/adherence.py` | 33% | see below |
| `coach/knowledge.py` | 100% (sample of 190) | n/a |

## Known gaps (not covered, in rough priority order)

1. **The real model.** Model-written plans in these tests came from sub-agents of the development session, not from `claude-sonnet-5-5` through the API. Run
   `python tools/plan_eval.py live OUT && python tools/plan_eval.py audit OUT` with a key, several times (the model varies), after any prompt or skeleton change.
   A nightly job that runs this and fails on rule or shape problems is the single most valuable next step.
2. **Session content quality.** Code checks distance, timing, spacing, caps, repetition and stacking. Whether a given tempo or interval session is a *good* session
   (reps, recoveries, progression of paces) depends on the prompt and the model; only an expert review catches it. The prompt now asks for progression, a 10%/8%
   cap on threshold/interval pace work and gradual run/walk progression, but nothing in code verifies those.
3. **Beginner run/walk progression** is prompt-only (no parser for "4:1 run/walk").
4. **Real Strava data.** Treadmill runs without moving time, duplicate or merged activities, GPS glitches, activities recorded in another time zone, and users
   whose Strava history starts mid-plan are covered only by synthetic data.
5. **Fitness estimation** (goal realism warning) uses the best recent run; mostly-easy running understates fitness, so the warning only fires on a very large gap (>20%).
6. **Outcomes.** The caps (run sizes, ramp rates, long-run shares) come from published plans and design choices. Nothing measures whether athletes following them are
   injured less or finish faster; that needs real users.
7. **Concurrency and operations**: two simultaneous jobs for one user, several worker processes, a database restored from backup mid-plan.
8. **Visual regression**: the plan screens are checked by hand and by text assertions, not by screenshot comparison.
9. **Races longer than a marathon** are refused at setup (3 km to 42.5 km); ultra training is a different problem.
