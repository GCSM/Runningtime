from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth
from .config import Settings
from .errors import explain
from .db import DB
from .llm import Coach
from .models import DAYS, KM_PER_MI, DayAvail, Profile
from .service import CoachService, UserError
from .strava import StravaClient, StravaError

log = logging.getLogger("coach")
HERE = Path(__file__).parent
PRESETS = {"5K": 5.0, "10K": 10.0, "Half marathon": 21.0975, "Marathon": 42.195}
PUBLIC_PATHS = {"/login", "/signup", "/healthz"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


def create_app(settings: Settings | None = None, db: DB | None = None, coach=None, strava_factory=None, clock=None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = db or DB(settings.db_path)
    coach = coach or Coach(settings.model, effort=settings.effort)
    strava_factory = strava_factory or (lambda udb: StravaClient(settings, udb))

    def service_for(uid: int) -> CoachService:
        udb = db.for_user(uid)
        return CoachService(udb, settings, coach, strava_factory(udb), clock=clock)

    failures: dict[str, list[float]] = defaultdict(list)  # crude login throttle: key -> timestamps

    def throttled(key: str) -> bool:
        now = time.time()
        failures[key] = [t for t in failures[key] if now - t < 600]
        return len(failures[key]) >= 8

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        def tick_all():
            for uid in db.strava_user_ids():
                try:
                    service_for(uid).background_tick()
                except Exception:
                    log.exception("background tick failed for user %s", uid)

        async def loop():
            while True:
                await asyncio.sleep(settings.sync_interval_min * 60)
                await asyncio.to_thread(tick_all)

        task = asyncio.create_task(loop()) if settings.sync_interval_min > 0 else None
        yield
        if task:
            task.cancel()

    app = FastAPI(lifespan=lifespan, title="Running Coach")
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))

    def dist(km, units="km", unit=True):
        if km is None:
            return "–"
        v = km / KM_PER_MI if units == "mi" else km
        return f"{v:.1f}" + (f" {units}" if unit else "")

    def pace(s, units="km"):
        if not s:
            return "–"
        s = s * KM_PER_MI if units == "mi" else s
        return f"{int(s // 60)}:{int(s % 60):02d}/{units}"

    def fmt_date(v, fmt="%a %b %-d"):
        return (v if isinstance(v, date) else date.fromisoformat(v)).strftime(fmt)

    def hms(s):
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s else ""

    def fmt_ts(iso):
        if not iso:
            return "never"
        from datetime import datetime
        from zoneinfo import ZoneInfo

        t = datetime.fromisoformat(iso).astimezone(ZoneInfo(settings.timezone))
        return t.strftime("%b %-d, %-I:%M %p")

    templates.env.filters.update(dist=dist, pace=pace, fmt_date=fmt_date, hms=hms, fmt_ts=fmt_ts)

    def render(request: Request, name: str, **ctx):
        return templates.TemplateResponse(
            request, name,
            {"flash": request.query_params.get("msg"), "error": request.query_params.get("err"),
             "user": getattr(request.state, "user", None), "path": request.url.path, **ctx},
        )

    def back(path: str, msg: str | None = None, err: str | None = None):
        q = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
        return RedirectResponse(f"{path}?{q}" if q else path, status_code=303)

    def set_session(resp, uid: int):
        resp.set_cookie(auth.COOKIE, auth.new_session(db, uid), max_age=auth.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=settings.secure_cookies, path="/")
        return resp

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if request.method == "POST":  # same-origin only (cookies are also SameSite=Lax)
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
                return PlainTextResponse("Cross-origin request blocked", 403)
        if path == "/healthz":
            return PlainTextResponse("ok")
        token = request.cookies.get(auth.COOKIE)
        user = db.session_user(auth.token_hash(token)) if token else None
        request.state.user = user
        if path.startswith("/static") or path in ("/login", "/signup"):
            if user and path in ("/login", "/signup") and request.method == "GET":
                return RedirectResponse("/", 303)
            return await call_next(request)
        if not user:
            return RedirectResponse("/login", 303)
        request.state.svc = service_for(user["id"])
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def action(fn, ok_msg, path="/"):
        try:
            res = fn()
            return back(path, ok_msg(res) if callable(ok_msg) else ok_msg)
        except (UserError, StravaError) as e:
            return back(path, err=str(e))
        except Exception as e:
            log.exception("action failed")
            return back(path, err=explain(e) or f"Something went wrong: {e}")

    # ---- accounts ----------------------------------------------------------
    @app.get("/login")
    def login_page(request: Request):
        return render(request, "login.html", mode="login", settings=settings)

    @app.post("/login")
    def login(request: Request, email: str = Form(""), password: str = Form("")):
        key = f"{request.client.host if request.client else '?'}|{email.lower()}"
        if throttled(key):
            return back("/login", err="Too many attempts. Try again in a few minutes.")
        u = db.user_by_email(email)
        if not u or not auth.verify_password(password, u["pw_hash"]):
            failures[key].append(time.time())
            return back("/login", err="Wrong email or password.")
        return set_session(RedirectResponse("/", 303), u["id"])

    @app.get("/signup")
    def signup_page(request: Request):
        return render(request, "login.html", mode="signup", settings=settings)

    @app.post("/signup")
    def signup(name: str = Form(""), email: str = Form(""), password: str = Form(""), invite: str = Form("")):
        if not settings.allow_signups:
            return back("/signup", err="Signups are closed.")
        if settings.invite_code and not secrets.compare_digest(invite.strip(), settings.invite_code):
            return back("/signup", err="That invite code isn't right.")
        if not EMAIL_RE.match(email.strip()):
            return back("/signup", err="Enter a valid email address.")
        if len(password) < 8:
            return back("/signup", err="Use a password of at least 8 characters.")
        if db.user_by_email(email):
            return back("/signup", err="An account with that email already exists.")
        uid = db.create_user(email, name or email.split("@")[0], auth.hash_password(password))
        return set_session(RedirectResponse("/setup", 303), uid)

    @app.post("/logout")
    def logout(request: Request):
        tok = request.cookies.get(auth.COOKIE)
        if tok:
            db.delete_session(auth.token_hash(tok))
        resp = RedirectResponse("/login", 303)
        resp.delete_cookie(auth.COOKIE, path="/")
        return resp

    @app.post("/account/delete")
    def delete_account(request: Request, confirm: str = Form("")):
        if confirm.strip().lower() != request.state.user["email"].lower():
            return back("/me", err="Type your email address to confirm deletion.")
        db.delete_user(request.state.user["id"])
        resp = RedirectResponse("/login", 303)
        resp.delete_cookie(auth.COOKIE, path="/")
        return resp

    # ---- pages -----------------------------------------------------------
    @app.get("/")
    def index(request: Request):
        d = request.state.svc.dashboard()
        if not d["profile"]:
            return RedirectResponse("/setup", 303)
        return render(request, "today.html", d=d, units=d["profile"].units, settings=settings)

    @app.get("/plan")
    def plan(request: Request):
        svc = request.state.svc
        p = svc.db.get_profile()
        return render(request, "plan.html", weeks=svc.full_plan(), units=p.units if p else "km", today=svc.today().isoformat(), has_profile=bool(p))

    @app.get("/progress")
    def progress(request: Request):
        svc = request.state.svc
        p = svc.db.get_profile()
        return render(request, "progress.html", pr=svc.progress(), units=p.units if p else "km")

    @app.get("/me")
    def me(request: Request):
        svc = request.state.svc
        p = svc.db.get_profile()
        import ipaddress
        from urllib.parse import urlparse

        host = urlparse(settings.public_url).hostname or ""
        try:
            ipaddress.ip_address(host)
            is_ip = host not in ("127.0.0.1", "::1")
        except ValueError:
            is_ip = False
        return render(request, "me.html", d=svc.dashboard(), p=p, units=p.units if p else "km", settings=settings,
                      callback_host=host, callback_is_ip=is_ip, callback_url=f"{settings.public_url}/strava/callback")

    @app.get("/setup")
    def setup(request: Request):
        return render(request, "setup.html", p=request.state.svc.db.get_profile(), days=DAYS, presets=PRESETS)

    @app.post("/setup")
    async def setup_save(request: Request):
        svc = request.state.svc
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
                name=request.state.user["name"], goal_label=label, goal_km=goal_km, race_date=date.fromisoformat(str(f.get("race_date"))),
                goal_time_s=parse_hms(str(f.get("goal_time", ""))), baseline_km=float(f.get("baseline") or 0) * to_km,
                experience=f.get("experience", "intermediate"), availability=avail, long_run_day=str(f.get("long_run_day", "sun")),
                notes=str(f.get("notes", "")), units=units,
            )
            if p.weekly_minutes == 0:
                raise ValueError("pick at least one day you can run, with minutes available")
            if not avail[p.long_run_day].available:
                raise ValueError("your long-run day must be one of your running days")
            if p.race_date <= svc.today():
                raise ValueError("the race date must be in the future")
        except Exception as e:
            return back("/setup", err=f"Check your inputs: {e}")
        had_plan = bool(svc.db.get_workouts())
        svc.db.save_profile(p)
        return back("/", "Saved. Tap “Review & adjust” to re-plan around the changes." if had_plan else "Saved. Now let’s build your plan.")

    # ---- actions ---------------------------------------------------------
    @app.post("/plan/generate")
    def generate(request: Request):
        return action(request.state.svc.generate_plan, "Your plan is ready.")

    @app.post("/review")
    def review(request: Request, note: str = Form("")):
        return action(lambda: request.state.svc.review(note.strip()), "Plan reviewed and updated.")

    @app.post("/sync")
    def sync(request: Request):
        return action(request.state.svc.sync, lambda n: f"Synced from Strava ({n} new run{'s' * (n != 1)}).", "/me")

    # ---- strava oauth ----------------------------------------------------
    @app.get("/strava/connect")
    def strava_connect(request: Request):
        svc = request.state.svc
        if not getattr(svc.strava, "configured", False):
            return back("/me", err="Add your Strava app's Client ID and Secret first.")
        state = secrets.token_urlsafe(16)
        svc.db.kv_set("strava_state", state)
        return RedirectResponse(svc.strava.authorize_url(state))

    @app.get("/strava/callback")
    def strava_callback(request: Request, code: str = "", error: str = "", scope: str = "", state: str = ""):
        svc = request.state.svc
        expected = svc.db.kv_get("strava_state")
        svc.db.kv_delete("strava_state")
        if not expected or not secrets.compare_digest(state, expected):
            return back("/me", err="Strava connection expired. Please try again.")
        if error or not code:
            return back("/me", err="Strava authorization was cancelled.")
        if "activity:read" not in scope:
            return back("/me", err="Please allow access to your activities on Strava's consent screen.")
        try:
            svc.strava.exchange_code(code)
            n = svc.sync()
        except Exception as e:
            return back("/me", err=f"Strava connection failed: {e}")
        return back("/me", f"Strava connected; imported {n} recent runs.")

    @app.post("/strava/app")
    def strava_app_save(request: Request, client_id: str = Form(""), client_secret: str = Form("")):
        cid, sec = client_id.strip(), client_secret.strip()
        if not cid.isdigit():
            return back("/me", err="The Client ID should be a number (shown on your Strava API settings page).")
        if len(sec) < 16 or not sec.isalnum():
            return back("/me", err="That Client Secret doesn't look right: it's a long string of letters and digits, with no spaces.")
        request.state.svc.strava.save_app(cid, sec)
        return back("/me", "Saved your Strava app. Now tap Connect with Strava.")

    @app.post("/strava/app/remove")
    def strava_app_remove(request: Request):
        request.state.svc.strava.remove_app()
        return back("/me", "Removed your Strava app and disconnected Strava.")

    @app.post("/strava/disconnect")
    def strava_disconnect(request: Request):
        request.state.svc.strava.disconnect()
        return back("/me", "Strava disconnected.")

    return app
