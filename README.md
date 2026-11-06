# Running Coach

A self-hosted, multi-user AI running coach. Tell it your race, your date and when you can run; it builds a periodized
plan, then **re-plans as you actually train** by reading your runs from Strava. Mobile-first UI (installable on iPhone
via Safari → Share → Add to Home Screen), with light and dark mode.

## How it works

```
 Strava ──sync──▶ activities ──match──▶ planned vs. actual ──┐
                                                             ▼
 goal + schedule ─▶ skeleton (code) ──▶ Claude ──▶ sanitize (code) ──▶ plan
                    phases, weekly km,    picks workouts,      only your days, your
                    ramp, taper           paces, explains      minutes, ≤ weekly cap
```

- **Code owns safety.** `skeleton.py` computes weekly volume (base → build → peak → taper, ~8% ramp,
  a recovery week every 4th week, long-run caps). `plancheck.py` forces whatever Claude returns to respect your
  available days, per-day minutes and the weekly cap. A hallucinating model can't schedule 60 km in a week.
- **Claude owns coaching.** Workout types, session design, paces (from your goal time and your real recent pace),
  and an honest note to you about what changed and why.
- **Adaptation.** On every review the skeleton is *re-derived from what you really ran* (mean of the last 4 complete
  weeks), so missed weeks pull targets down and consistent weeks let it progress. Claude also sees per-workout
  planned vs. actual, pace, HR, unplanned runs, and your free-text check-in ("calf felt tight").
- **Automatic.** A background task syncs Strava every `SYNC_INTERVAL_MIN` and reviews the plan once at the start of each
  week (`AUTO_ADJUST=true`). You can also hit **Sync Strava & adjust plan** any time.

## Run it

```bash
cp .env.example .env   # add ANTHROPIC_API_KEY (+ Strava, below)
docker compose up -d --build
# open http://localhost:8000  → "Goal & schedule" → "Generate my plan"
```

Without Docker: `pip install -r requirements.txt && uvicorn coach.web:create_app --factory`.

### Accounts
Anyone can sign up with an email and password (passwords are hashed with scrypt; sessions are HttpOnly cookies).
Every plan, run, and Strava connection is private to its user. On a server others can reach, restrict signups:
`INVITE_CODE=somesecret` requires a code at signup; `ALLOW_SIGNUPS=false` closes signup once everyone has joined.
Users can delete their account and all their data from **Me**. Serve over HTTPS (reverse proxy or Tailscale) and set
`PUBLIC_URL` to the `https://` address so cookies are marked `Secure`.

### Strava
1. Create an app at <https://www.strava.com/settings/api>.
2. Set **Authorization Callback Domain** to the host in `PUBLIC_URL`.
3. Put the client id/secret in `.env`, restart. Each user then taps **Connect with Strava** under **Me**
   (scope `read,activity:read_all`) and their tokens are stored separately.
4. Strava limits a new API app to a small number of connected athletes until you request a higher limit
   (see your app's page on Strava). Check this before inviting more than a couple of people.

No Strava? The planner still works from your self-reported weekly mileage; you just won't get automatic adaptation.

The server's `ANTHROPIC_API_KEY` is shared by all users, so every plan or review is billed to you. Keep signups
restricted accordingly.

## Develop
```bash
pip install -r requirements-dev.txt && pytest
```
The tests use a fake coach and fake Strava, so they need no network or API key.

## Ideas for next
Strava webhooks (instant sync instead of polling) · Discord bot front-end (`/today`, `/checkin`) calling the same
`CoachService` · push the day's workout to a calendar feed · HR-zone based paces · cross-training / strength days.
