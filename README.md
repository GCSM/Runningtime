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
- **Never makes you wait.** Building a plan, reviewing and syncing run as background jobs: the page returns instantly, a
  banner shows progress while you keep browsing, and when it finishes you get a toast and the page updates itself.
  (One job per user at a time; a second tap just joins the running one.)
- **Automatic.** A background task syncs Strava every `SYNC_INTERVAL_MIN` and reviews the plan once at the start of each
  week (`AUTO_ADJUST=true`). You can also hit **Sync Strava & adjust plan** any time.

## Where the numbers come from

Plans are built from published coaching material, not guesses. [docs/RESEARCH.md](docs/RESEARCH.md) records what published
plans and coaching research say, with sources and a confidence rating for each fact (weekly volume by goal and level,
progression and recovery weeks, long runs, intensity, taper, plan length, beginners, pace calculation, workout types,
returning from a layoff). `coach/knowledge.py` encodes it as data, and every constant traces to a row in that document.

- **Volume** targets published peak ranges for the athlete's goal and level (a 5K plan for an intermediate runner peaks
  around 30–46 km/week, not 15), reached by a level-based ramp from where the athlete *actually is* (Strava, else the number
  they typed, else a stated assumption). The Plan tab shows start → peak and says plainly when schedule or time-to-race is the limit.
- **Paces** (easy, marathon, threshold, interval, repetition) are computed in code from the goal time with the Daniels/Gilbert
  VDOT equations (checked against published tables), never by the language model.
- **Rules enforced in code** on whatever the model returns: hard-session limits by level and phase, an easy day between hard
  sessions and either side of the long run, no hard work in recovery weeks, long-run size/time/growth caps, hard sessions at most
  45% of the week's distance, under-delivering weeks topped up, no hard sessions for a week after a pain report.
- Limits of the research (several primary sources could not be read directly, and published plans disagree by up to 2×) are
  listed at the bottom of the research doc. This is general fitness guidance, not medical advice.

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

### Strava (one app for the server; each user just connects)
You create **one** Strava app, and every user taps **Connect with Strava** on the Me tab. Nobody else needs a developer account.
1. Create the app at <https://www.strava.com/settings/api>.
2. Set **Authorization Callback Domain** to the host you browse to, exactly (e.g. `coach.lan`: no `http://`, no port, no slash).
   The Me tab shows the exact value and the callback URL being sent.
3. Put the Client ID and Secret in the server config (`bash deploy/reconfigure.sh`) and set `STRAVA_ATHLETE_LIMIT` to the athlete
   capacity Strava gave the app. New apps start at 1; one third-party guide says you can raise it to 10 yourself in the app's
   settings (unconfirmed), and more needs Strava's review.

How it behaves:
- Each user's tokens are stored separately; users can disconnect any time, which also revokes the app's access on Strava.
- **The limit is enforced here too**: once `STRAVA_ATHLETE_LIMIT` users are connected, the next person is told the app is full
  *before* being sent to Strava. Disconnecting or deleting an account frees the slot locally. Whether Strava also frees its own
  slot on revoke is unconfirmed, so if you hit Strava's limit despite free local slots, remove stale athletes in Strava's settings.
- If someone revokes access on Strava's side, the next sync notices, clears the connection and asks them to reconnect.
- Rate limits are handled: a throttled sync reports it and retries on the next cycle.
- Webhooks (instant "new run" pushes) aren't used because Strava's servers can't reach a home-network address; runs sync every
  `SYNC_INTERVAL_MIN` minutes instead.

No Strava? The planner still works from your self-reported weekly mileage; you just won't get automatic adaptation.

The server's `ANTHROPIC_API_KEY` is shared by all users, so every plan or review is billed to you. Keep signups
restricted accordingly.

### Coach chat (cheapest model)

The **Coach** tab answers quick questions ("what pace for easy runs?", "my knee hurts") using **Claude Haiku 5.5**
at low effort, with a compact context (about 1k tokens) and short replies, so a message costs a fraction of a cent
(estimate, not measured). It is capped per user (`CHAT_MESSAGES_PER_DAY`, default 40, rolling 24h). The model can
*suggest* skipping, moving or swapping workouts, easing off, or a re-plan, but nothing changes until you tap **Apply**;
every action is validated against your real plan, again when applied. Mentions of pain always produce an ease-off
suggestion. Settings: `COACH_CHAT_MODEL`, `COACH_CHAT_EFFORT`, `CHAT_MESSAGES_PER_DAY`.

### Crew, appearance and offline

- **Crew** (Me → Crew): opt in to see friends on the same server. Shared: first name, this week's distance and run count,
  streak, goal and days to race. Never routes, paces, heart rate, feedback or chats. One cheer (👏🔥💪) per person per week.
  Leaving removes you immediately; you can't see the crew unless you're in it.
- **Appearance** (Me): Auto / Light / Dark, remembered per device.
- **First run** is a short guided setup; returning users get the full form.
- **Pull down** on Today/Plan/Progress/Me to sync Strava (or refresh if not connected).
- **Offline**: browsers only allow service workers on HTTPS or localhost, so on a plain-HTTP LAN the app simply stays
  online-only. Behind HTTPS it caches the static files and your last-viewed tabs, and wipes them when you sign out.

## Develop
```bash
pip install -r requirements-dev.txt && pytest
```
The tests use a fake coach and fake Strava, so they need no network or API key (running them costs nothing).
`tests/test_knowledge.py` pins the constants to the research; `tests/test_scenarios.py` checks the weekly volume across 2,700
athlete setups, feeds deliberately bad plans through the rule checker, and runs the whole service end to end.

## Ideas for next
Strava webhooks (instant sync instead of polling) · Discord bot front-end (`/today`, `/checkin`) calling the same
`CoachService` · push the day's workout to a calendar feed · HR-zone based paces · cross-training / strength days.
