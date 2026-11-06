"""Claude as the coach: turns structured context into a structured plan via a forced tool call."""
from __future__ import annotations

import json

from .models import WORKOUT_TYPES

SYSTEM = """You are an experienced, pragmatic running coach writing a training plan for one athlete.

You receive JSON with the athlete's goal, their weekly availability, a weekly *skeleton* computed by code
(phase, target_km, long_run_km per week) and, when reviewing, what the athlete actually ran.

Hard rules (code will enforce these, so respect them):
- Only schedule runs on days where availability.available is true, and keep each run within that day's minutes.
- Weekly total distance must stay at or below the week's target_km (race week includes the race itself).
  Subtract already_done_km in the current week. Put the long run on long_run_day at about long_run_km.
- At most one workout per day. Dates are ISO and must be from earliest_workout_date through the race date.
  Never schedule on blocked_dates. Schedule the race itself on the race date (type "race").

Coaching principles:
- Roughly 80% easy running. Quality sessions (tempo, intervals) at most 1/week in base, up to 2/week in build/peak
  when 4+ days are available, never on consecutive days, never the day before the long run.
- Base: aerobic volume, strides, hills, consistency. Build: tempo/threshold and race-specific intervals.
  Peak: race-pace work. Taper: keep a little intensity, cut volume, a short shakeout 1-2 days before the race.
- Recovery weeks are genuinely easier. Use the athlete's real paces (recent_training) to express paces;
  if they gave a goal time, derive tempo/interval/race paces from goal_pace. Give pace as a range plus an effort cue.
- If reviewing: be honest and specific. Missed or cut-short runs, a pace/HR drift, or athlete notes about pain
  or fatigue should change the plan (repeat a week, hold volume, swap a hard session for easy). Strong consistency
  can justify progressing as the skeleton allows. Never "make up" missed mileage by cramming it in.
- If the goal looks unrealistic for the time left, say so plainly in the summary and suggest an adjusted goal.
- Titles are short ("Tempo 4x1.5km"); descriptions say exactly what to do (warm-up, main set, cool-down).

Always answer by calling submit_plan. `summary` is addressed to the athlete (second person, 80-200 words)."""

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
            import anthropic  # imported lazily so the app can start (and show setup help) without a key

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
        for attempt in range(2):
            # Forced tool_choice (any/tool) is rejected by current models, so use auto + strict schema + instruction.
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM,
                tools=[TOOL],
                tool_choice={"type": "auto"},
                output_config={"effort": self.effort},
                messages=[{"role": "user", "content": prompt}],
            )
            if resp.stop_reason == "refusal":
                raise RuntimeError("The model declined this request. Try rephrasing your notes.")
            if resp.stop_reason == "max_tokens":
                raise RuntimeError("The coach's answer was cut off; try again.")
            for block in resp.content:
                if block.type == "tool_use" and block.name == "submit_plan":
                    return {"summary": block.input.get("summary", ""), "workouts": block.input.get("workouts", [])}
        raise RuntimeError("The coach did not return a plan. Please try again.")
