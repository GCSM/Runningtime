from __future__ import annotations

import asyncio
import base64
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import Settings
from .db import DB
from .llm import Coach
from .models import DAYS, KM_PER_MI, DayAvail, Profile
from .service import CoachService, UserError
from .strava import StravaClient, StravaError

log = logging.getLogger("coach")
HERE = Path(__file__).parent
PRESETS = {"5K": 5.0, "10K": 10.0, "Half marathon": 21.0975, "Marathon": 42.195}


def parse_hms(s: str) -> int | None:
    s = s.strip()
    if not s:
        return None
    parts = [int(x) for x in s.split(":")]
    if len(parts) == 2:
        parts = [0] + parts
    if len(parts) != 3:
        raise ValueError("Use h:mm:ss or mm:ss")
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def create_app(settings: Settings | None = None, service: CoachService | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if service is None:
        db = DB(settings.db_path)
        service = CoachService(db, settings, Coach(settings.model), StravaClient(settings, db))
    svc = service

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def loop():
            while True:
                await asyncio.sleep(settings.sync_interval_min * 60)
                try:
                    await asyncio.to_thread(svc.background_tick)
                except Exception:  # keep the loop alive
                    log.exception("background tick failed")

        task = asyncio.create_task(loop()) if settings.sync_interval_min > 0 else None
        yield
        if task:
            task.cancel()

    app = FastAPI(lifespan=lifespan, title="Running Coach")
    templates = Jinja2Templates(directory=str(HERE / "templates"))

    def dist(km, units="km"):
        if km is None:
            return "–"
        return f"{km / KM_PER_MI:.1f} mi" if units == "mi" else f"{km:.1f} km"

    def pace(s, units="km"):
        if not s:
            return "–"
        s = s * KM_PER_MI if units == "mi" else s
        return f"{int(s // 60)}:{int(s % 60):02d}/{units}"

    def dayname(iso):
        return date.fromisoformat(iso).strftime("%a %b %-d")

    templates.env.filters.update(dist=dist, pace=pace, dayname=dayname)

    def render(request: Request, name: str, **ctx):
        return templates.TemplateResponse(request, name, {"flash": request.query_params.get("msg"), "error": request.query_params.get("err"), **ctx})

    def back(path: str, msg: str | None = None, err: str | None = None):
        from urllib.parse import urlencode

        q = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
        return RedirectResponse(f"{path}?{q}" if q else path, status_code=303)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path == "/healthz":
            return PlainTextResponse("ok")
        if settings.app_password:
            ok = False
            h = request.headers.get("authorization", "")
            if h.lower().startswith("basic "):
                try:
                    pw = base64.b64decode(h[6:]).decode().partition(":")[2]
                    ok = secrets.compare_digest(pw, settings.app_password)
                except Exception:
                    ok = False
            if not ok:
                return PlainTextResponse("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Running Coach"'})
        if request.method == "POST":  # basic CSRF defence: same-origin only
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
                return PlainTextResponse("Cross-origin request blocked", 403)
        return await call_next(request)

    def action(fn, ok_msg, path="/"):
        try:
            res = fn()
            return back(path, ok_msg(res) if callable(ok_msg) else ok_msg)
        except UserError as e:
            return back(path, err=str(e))
        except StravaError as e:
            return back(path, err=str(e))
        except Exception as e:
            log.exception("action failed")
            return back(path, err=f"Something went wrong: {e}")

    # ---- pages -----------------------------------------------------------
    @app.get("/")
    def index(request: Request):
        d = svc.dashboard()
        if not d["profile"]:
            return RedirectResponse("/setup", 303)
        return render(request, "dashboard.html", d=d, units=d["profile"].units, settings=settings)

    @app.get("/plan")
    def plan(request: Request):
        p = svc.db.get_profile()
        return render(request, "plan.html", weeks=svc.full_plan(), units=p.units if p else "km", today=svc.today().isoformat())

    @app.get("/setup")
    def setup(request: Request):
        return render(request, "setup.html", p=svc.db.get_profile(), days=DAYS, presets=PRESETS)

    @app.post("/setup")
    async def setup_save(request: Request):
        f = await request.form()
        try:
            units = f.get("units", "km")
            to_km = KM_PER_MI if units == "mi" else 1.0
            preset = f.get("goal_preset", "")
            goal_km = PRESETS[preset] if preset in PRESETS else float(f.get("goal_custom") or 0) * to_km
            label = preset if preset in PRESETS else f"{f.get('goal_custom')} {units}"
            avail = {
                d: DayAvail(available=f.get(f"av_{d}") == "on", minutes=int(f.get(f"min_{d}") or 0), time=str(f.get(f"time_{d}") or ""))
                for d in DAYS
            }
            p = Profile(
                name=str(f.get("name", "")), goal_label=label, goal_km=goal_km, race_date=date.fromisoformat(str(f.get("race_date"))),
                goal_time_s=parse_hms(str(f.get("goal_time", ""))), baseline_km=float(f.get("baseline") or 0) * to_km,
                experience=f.get("experience", "intermediate"), availability=avail, long_run_day=str(f.get("long_run_day", "sun")),
                notes=str(f.get("notes", "")), units=units,
            )
            if p.weekly_minutes == 0:
                raise ValueError("Pick at least one day you can run, with minutes available.")
            if not avail[p.long_run_day].available:
                raise ValueError("Your long-run day must be one of your available days.")
            if p.race_date <= svc.today():
                raise ValueError("The race date must be in the future.")
        except Exception as e:
            return back("/setup", err=f"Check your inputs: {e}")
        svc.db.save_profile(p)
        return back("/", "Saved. Now generate your plan." if not svc.db.get_workouts() else "Saved. Hit “Review & adjust” to re-plan with the new settings.")

    # ---- actions ---------------------------------------------------------
    @app.post("/plan/generate")
    def generate():
        return action(svc.generate_plan, "Plan created.")

    @app.post("/review")
    def review(note: str = Form("")):
        return action(lambda: svc.review(note.strip()), "Plan reviewed and updated.")

    @app.post("/sync")
    def sync():
        return action(svc.sync, lambda n: f"Synced from Strava ({n} new run{'s' * (n != 1)}).")

    # ---- strava oauth ----------------------------------------------------
    @app.get("/strava/connect")
    def strava_connect():
        if not settings.strava_configured:
            return back("/", err="Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET first.")
        return RedirectResponse(svc.strava.authorize_url())

    @app.get("/strava/callback")
    def strava_callback(code: str = "", error: str = "", scope: str = ""):
        if error or not code:
            return back("/", err="Strava authorization was cancelled.")
        if "activity:read" not in scope and "activity:read_all" not in scope:
            return back("/", err="Please grant access to your activities on Strava's consent screen.")
        try:
            svc.strava.exchange_code(code)
            n = svc.sync()
        except Exception as e:
            return back("/", err=f"Strava connection failed: {e}")
        return back("/", f"Strava connected; imported {n} recent runs.")

    return app
