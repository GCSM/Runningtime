"""Claude as the coach: turns structured context into a structured plan via a forced tool call."""
from __future__ import annotations

import json

from .models import WORKOUT_TYPES

SYSTEM = """You are an experienced, pragmatic running coach writing a training plan for one athlete.

You receive JSON with the athlete's goal and level, their weekly availability, and a weekly *skeleton* computed by code
(phase, target_km, long_run_km, max_quality_sessions per week). `volume_plan` says where the athlete starts and how far the
build goes, and why. `training_paces_per_km` are paces computed in code from the goal time (or recent runs): copy them
exactly into pace_guidance and never do pace arithmetic yourself. `workout_menu_by_phase` lists standard sessions for
each phase of this goal: pick from it and scale it to the athlete.

Hard rules (code enforces these and will rewrite anything that breaks them):
- Run only on days where availability.available is true, within that day's minutes. One workout per day. Dates are ISO and
  run from earliest_workout_date to the race date. Never use blocked_dates. The race goes on the race date (type "race").
- Hit each week's target_km: aim for 90-100% of it (subtract already_done_km in the current week). Under-delivering the
  target is a failure, as is exceeding it by more than 10%. Spread the volume across all available running days.
- Long run on long_run_day at about long_run_km. Grow it gradually; recovery weeks and taper weeks are shorter.
- The race week's target_km INCLUDES the race itself: around it plan only short easy runs, strides and a shakeout. Never plan more than
  one or two light efforts in race week.
- Hard sessions (type tempo or intervals): at most max_quality_sessions in the week, at least one easy day between them,
  never the day before or after the long run, none in a recovery week. Strides added to an easy run do not count.

Units: `athlete.units`/`units` says whether the athlete thinks in kilometres or miles. All numeric fields in your answer (distance_km, duration_min)
stay in kilometres and minutes, but titles, descriptions and pace_guidance are read by the athlete: if units is "mi", write distances in miles
("Tempo 5 mi", "3 x 1 mi") and use the *_per_mile paces; if "km", use kilometres and the *_per_km paces. Never mix them in one text.
Workout types are fixed, so use them consistently: hill repeats and fartlek are "intervals"; any block at marathon or half-marathon pace
(including taper sessions at goal pace) is "tempo"; a shakeout or strides run is "easy". Both count toward max_quality_sessions. A long run
with a pace finish stays "long" and does not count as hard. Midweek "medium-long" runs from the menu only fit if the day's minutes allow.
For duration_min of easy and long runs use distance_km x easy_pace_s_per_km_estimate / 60; use the training paces for quality sessions.
If `race_paces_per_km` is present, use it for "5K pace", "10K pace", "half-marathon pace" and "goal pace" in menu items.
`recent_training` showing no Strava history means "unknown", not "zero": volume_plan.baseline_km is what the athlete currently runs.

Hard efforts and progression (checked by code afterwards, so plan them properly):
- A long run with a pace finish (marathon, half-marathon, tempo or progression) is a hard effort. At most two hard efforts (tempo, intervals,
  paced long run) in any 7 days. As above, keep at least one easy or rest day between any two hard efforts (a rest day counts).
- Threshold-pace work adds up to at most ~10% of the week's distance (12% in a marathon block) and interval-pace work to at most 8%; the rest of
  a quality session is warm-up, jogs and cool-down.
- Quality sessions must progress: add reps or minutes by ~10-15% across the build, step back in recovery weeks, alternate threshold and
  interval-type stimuli, and never repeat an identical session more than twice in a row. Race-specific work grows through the peak phase.
- Run/walk beginners: lengthen the running segments by at most 1-2 minutes (or about 25%) a week, never start continuous running and a quality
  session in the same week, and set distance = time / the average pace *including the walks* so it matches what the session really yields.
- Race week: short easy runs and strides, plus (if the week's km allow) one brief goal-pace touch 2-3 days before the race. Nothing hard the last two days.

Coaching principles (from published plans and coaching research):
- About 80% of running is easy: conversational, RPE 3-4 (talk test). Beginners tend to run easy days too fast; say so.
- Base: aerobic volume, strides, hills, fartlek, consistency. Build: threshold and race-specific intervals. Peak: race-pace
  work and a race simulation. Taper: cut volume, keep short sharp efforts at goal pace. Shakeout 1-2 days before the race.
- A beginner whose current volume is low starts with run/walk intervals (3 runs a week with rest days between) and moves to
  continuous running over several weeks; advance only when the previous week felt manageable.
- Recovery weeks are genuinely easier and contain no hard sessions.
- Express paces as a range plus an effort cue. If training_paces_per_km is null, use effort only.
- If the goal pace is much faster than the athlete's recent running suggests, say so plainly and ease in; if the time left is
  too short for the goal, say that too and suggest an adjusted goal.
- If `layoff` is present, restart at the suggested share of previous volume and rebuild. If
  injury_or_pain_mentioned_in_check_in is true, plan no hard sessions for now, reduce volume, and tell the athlete to get it
  checked by a clinician if it persists; do not diagnose.
- When reviewing: be honest and specific. Missed or cut-short runs, pace or heart-rate drift, and athlete notes should change
  the plan. Never "make up" missed mileage. Strong consistency can justify progressing as the skeleton allows.
- Titles are short ("Tempo 4 x 1.5 km"); descriptions say exactly what to do (warm-up, main set, cool-down).

Always answer by calling submit_plan. `summary` is addressed to the athlete (second person, 80-200 words); do not repeat the
volume sentence, the app adds it."""

TOOL = {
    "name": "submit_plan",
    "description": "Submit the complete set of scheduled workouts and a short message to the athlete.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string", "description": "Message to the athlete: plan overview or review of recent training and what changed."},
            "workouts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "date": {"type": "string", "description": "YYYY-MM-DD"},
                        "type": {"type": "string", "enum": WORKOUT_TYPES},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "distance_km": {"type": "number"},
                        "duration_min": {"type": "integer"},
                        "pace_guidance": {"type": "string"},
                    },
                    "required": ["date", "type", "title", "description", "distance_km", "duration_min", "pace_guidance"],
                },
            },
        },
        "required": ["summary", "workouts"],
    },
}


class Coach:
    def __init__(self, model: str, client=None, effort: str = "medium"):
        self.model = model
        self.effort = effort
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import os

            import anthropic  # imported lazily so the app can start (and show setup help) without a key

            from .errors import CoachUnavailable

            if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
                raise CoachUnavailable(
                    "No Anthropic API key is configured. Add it with “bash deploy/reconfigure.sh”, then try again."
                )
            self._client = anthropic.Anthropic()
        return self._client

    def plan(self, context: dict, *, mode: str, feedback: list[str] | None = None) -> dict:
        """mode: 'generate' | 'adjust'. Returns {'summary': str, 'workouts': [dict]}."""
        task = (
            "Create the training plan from earliest_workout_date through race day."
            if mode == "generate"
            else "Review the recent training against the plan and rewrite the plan from earliest_workout_date through race day."
        )
        if feedback:
            task += "\n\nYour previous attempt was corrected by validation. Avoid these problems:\n- " + "\n- ".join(feedback[:12])
        task += "\n\nRespond by calling the submit_plan tool exactly once. Do not write any other text."
        prompt = f"{task}\n\n```json\n{json.dumps(context, indent=1)}\n```"
        short = ("\n\nIMPORTANT: your previous answer was cut off because it was too long. Keep every title under 8 words, every description to ONE short "
                 "sentence (a few words for easy runs), and every pace_guidance under 12 words. Cover every week through race day.")
        truncated = 0
        for attempt in range(3):
            if attempt >= 2 and not truncated:
                break                                                # one retry for a missing tool call; up to two for truncation
            # Forced tool_choice (any/tool) is rejected by current models, so use auto + strict schema + instruction.
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=20000 if truncated else 16000,        # (above ~21k the SDK insists on streaming)
                system=SYSTEM,
                tools=[TOOL],
                tool_choice={"type": "auto"},
                output_config={"effort": "low" if truncated else self.effort},      # thinking tokens count toward the cap: think less on the retry
                messages=[{"role": "user", "content": prompt + (short if truncated else "")}],
            )
            if resp.stop_reason == "refusal":
                raise RuntimeError("The model declined this request. Try rephrasing your notes.")
            if resp.stop_reason == "max_tokens":
                truncated += 1
                if attempt < 2:
                    continue                                         # a long plan (many weeks) can overflow: ask again, briefer and with less thinking
                raise RuntimeError("The coach's answer was cut off; try again.")
            for block in resp.content:
                if block.type == "tool_use" and block.name == "submit_plan":
                    return {"summary": block.input.get("summary", ""), "workouts": block.input.get("workouts", [])}
        raise RuntimeError("The coach did not return a plan. Please try again.")
