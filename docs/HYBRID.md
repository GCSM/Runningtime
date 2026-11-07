# Hybrid planning: code owns the plan, the model adds judgement and words

Status: design (not built). Evidence base: docs/QA.md (blind comparisons of the code-built plan against Sonnet 5.5 plans, two rounds; the builder
won 5/5 in round 2) and the planner survey (Garmin Coach, Pfitzinger, Hansons, Runna, TrainAsONE: structure from templates and rules, individualisation
from paces and adaptation; none lets a free-form model write the sessions).

## What each side is good at (measured, not assumed)
| Code (`builder.py`, skeleton, sanitizer) | Model (Sonnet / Haiku) |
|---|---|
| Structure: weekly volume, long run, hard-day spacing, taper, paced long-run blocks | Reading messy human input: "away Thu-Sun", "calf tight", "race got moved", "hate hills" |
| Every number: paces (VDOT), reps, distances, durations; units | Deciding *which* validated change fits the situation when several do |
| Rules and audits (reproducible, testable) | Wording: why this session, what to feel, pacing cues; weekly note; review of the last week |
| Pace adaptation from results and benchmark time trials | Variety of phrasing (the builder's text is repetitive) |
Sonnet's measured failures when it wrote the plan: no pace numbers, no half-marathon pace work in long runs, marathon-pace work only from week 16, one reckless
30 km run with 14 km at marathon pace three weeks out, text slips ("+ strides + strides").

## Contract: the model never writes numbers into the plan
The model returns **proposals**, never workouts. Code validates and applies them with the same machinery the chat feature already uses (confirm-to-apply).

1. **Explain** (`PlanNotes`): per-workout `why` (one sentence) and a per-week note, plus the plan summary. Stored as separate fields; the structured session text
   (warm-up, reps, paces) stays code-written, so the model cannot change or contradict it. Cheap model (Haiku) is enough.
2. **Interpret** (`PlanRequests`): the athlete's free text (check-in, notes, chat) becomes a list of typed requests from a closed set:
   `unavailable(dates)`, `move(date -> date)`, `ease(week|days, reason)`, `skip_hard(until)`, `swap_family(session_kind, avoid)` (e.g. no hills), `shift_race(date)`,
   `note(text)`. Each request is validated by code (dates in window, race untouched, spacing rules, volume bounds) and applied by the existing actions
   (`ease_week`, availability/blocked dates, `retitle`, rebuild). Anything not in the set is shown to the athlete as a note, never applied.
3. **Choose** (`PlanChoices`, optional later): where the builder has several valid variants (hills vs track, which of two long-run days) the model picks by id.

## Safety rules
- The sanitizer + `planaudit` still run after every application; a request that would violate a rule is dropped and the athlete is told why in plain words.
- No model call is on the critical path of producing a plan: if the model is unavailable, the plan is built and notes are filled in later ("your coach note is on its way").
- Pain/injury words keep the existing deterministic path (`mentions_pain`): hard sessions stop; the model only words the advice and says to see a clinician.
- Cost bound: notes are generated once per plan/review (<= ~3k output tokens); requests only when the athlete writes something.

## How we will know it is better (before switching anything)
1. **Request parsing**: a fixed set of ~60 realistic notes with expected typed requests (dates, scope); measure exact-match and, more importantly, *harmful-apply
   rate* (must be zero: validation catches every invalid one).
2. **Notes quality**: blind coach review of builder-only text vs builder + model notes on the same five scenarios (clarity, correctness, no contradiction with numbers).
   A contradiction check in code: any pace/distance in a note must appear in the workout it annotates.
3. **No regression**: the existing plan audits and the 765-test suite must stay identical when the model returns garbage, nothing, or an adversarial answer
   (fuzzed like the sanitizer).
4. **Benchmark loop** (not yet tested end to end): plan with a benchmark -> record a time-trial run -> next review's paces move; add a whole-service test.

## Build order
1. `PlanNotes` (Haiku): schema, prompt, storage, UI ("why this session"), contradiction check, fuzz tests. Smallest, safest, visible win.
2. `PlanRequests`: closed request set + validators over existing actions; wire to check-in and chat; the 60-note evaluation set.
3. Benchmark end-to-end test; recovery vs general-aerobic run labels; run-walk -> continuous transition rung.
4. Flip the default engine to `builder` once 1-3 pass and a final blind review agrees; keep `PLAN_ENGINE=model` as an escape hatch for one release.
