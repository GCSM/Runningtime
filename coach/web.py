from __future__ import annotations

import asyncio
import logging
import re
import itertools
import secrets
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth
from .chat import ChatCoach, ChatService
from .config import Settings
from .errors import explain
from .db import DB
from .llm import Coach
from .models import DAYS, KM_PER_MI, DayAvail, Profile
from .service import CoachService, UserError
from .strava import StravaClient, StravaError

log = logging.getLogger("coach")
HERE = Path(__file__).parent
RUN_LENGTHS = [("Short", 40), ("Medium", 60), ("Long", 90), ("Extra long", 150), ("Max long", 240), ("Open-ended", 300)]   # longest run that fits a given day, in minutes
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


def goal_seconds(f) -> int | None:
    """Target time from separate h / m / s boxes (a phone number pad has no colon); the old single h:mm:ss field still works."""
    parts = [str(f.get(k) or "").strip() for k in ("goal_h", "goal_m", "goal_s")]
    if any(parts):
        h, m, s = (int(x or 0) for x in parts)
        if min(h, m, s) < 0 or m > 59 or s > 59:
            raise ValueError("target time: minutes and seconds must be 0-59")
        return (h * 3600 + m * 60 + s) or None
    return parse_hms(str(f.get("goal_time", "")))


def create_app(settings: Settings | None = None, db: DB | None = None, coach=None, strava_factory=None, clock=None, chat=None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = db or DB(settings.db_path)
    coach = coach or Coach(settings.model, effort=settings.effort)
    chat_llm = chat or ChatCoach(settings.chat_model, settings.chat_effort)
    strava_factory = strava_factory or (lambda udb: StravaClient(settings, udb))

    def service_for(uid: int) -> CoachService:
        udb = db.for_user(uid)
        return CoachService(udb, settings, coach, strava_factory(udb), clock=clock)

    failures: dict[str, list[float]] = defaultdict(list)  # crude login throttle: key -> timestamps

    def throttled(key: str) -> bool:
        now = time.time()
        failures[key] = [t for t in failures[key] if now - t < 600]
        return len(failures[key]) >= 8

    # ---- background jobs: slow work (Claude, Strava) runs off the request so pages never block ----------
    job_ids = itertools.count(1)
    jobs: dict[int, dict] = {}          # user id -> most recent job
    jobs_lock = threading.Lock()
    JOB_STALE_S = 15 * 60               # a job "running" longer than this is reported as failed (e.g. thread hung)

    def start_job(uid: int, kind: str, label: str, fn, ok_msg) -> dict:
        """Start fn in a thread unless this user already has one running. Returns the (new or existing) job."""
        with jobs_lock:
            cur = jobs.get(uid)
            if cur and cur["status"] == "running" and time.time() - cur["started"] < JOB_STALE_S:
                return cur
            job = {"id": next(job_ids), "kind": kind, "label": label, "status": "running", "message": "", "started": time.time()}
            jobs[uid] = job

        def run():
            try:
                res = fn()
                status, msg = "done", (ok_msg(res) if callable(ok_msg) else ok_msg)
            except (UserError, StravaError) as e:
                status, msg = "error", str(e)
            except Exception as e:  # noqa: BLE001
                log.exception("%s job failed", kind)
                status, msg = "error", explain(e) or f"Something went wrong: {e}"
            with jobs_lock:
                job.update(status=status, message=msg, finished=time.time())
                if db.user_by_id(uid) is None:   # account was deleted while this ran: don't leave its data behind
                    db.delete_user(uid)
                    jobs.pop(uid, None)

        threading.Thread(target=run, name=f"job-{kind}-{uid}", daemon=True).start()
        return job

    def public_job(uid: int) -> dict:
        with jobs_lock:
            j = jobs.get(uid)
            if not j:
                return {"status": "idle"}
            status, msg = j["status"], j["message"]
            if status == "running" and time.time() - j["started"] >= JOB_STALE_S:
                status, msg = "error", "That took too long and was abandoned. Please try again."
            return {"id": j["id"], "kind": j["kind"], "label": j["label"], "status": status, "message": msg}

    def wants_json(request: Request) -> bool:
        return request.headers.get("x-requested-with") == "fetch" or "application/json" in request.headers.get("accept", "")

    def job_response(request: Request, uid: int, job: dict, path: str = "/"):
        """JSON for the page's script; a plain redirect (banner appears on load) for no-JS form posts."""
        if wants_json(request):
            return JSONResponse(public_job(uid))
        return RedirectResponse(path, 303)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        def tick_all():
            for uid in db.strava_user_ids():
                try:
                    service_for(uid).background_tick()
                except StravaError as e:
                    log.warning("strava sync skipped for user %s: %s", uid, e)
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

    app = FastAPI(lifespan=lifespan, title="RunningTime")
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

    def dur(sec):
        sec = int(round(sec or 0))
        return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"

    templates.env.filters.update(dist=dist, pace=pace, fmt_date=fmt_date, hms=hms, fmt_ts=fmt_ts, dur=dur)

    def render(request: Request, name: str, **ctx):
        svc = getattr(request.state, "svc", None)
        ctx.setdefault("strava_on", bool(svc and svc.strava.connected))
        return templates.TemplateResponse(
            request, name,
            {"flash": request.query_params.get("msg"), "error": request.query_params.get("err"),
             "user": getattr(request.state, "user", None), "path": request.url.path, **ctx},
        )

    def back(path: str, msg: str | None = None, err: str | None = None):
        q = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
        return RedirectResponse(f"{path}?{q}" if q else path, status_code=303)

    def set_session(resp, uid: int, request: Request):
        secure = settings.secure_cookies or request.url.scheme == "https"
        resp.set_cookie(auth.COOKIE, auth.new_session(db, uid), max_age=auth.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=secure, path="/")
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
        if path.startswith("/cal/"):
            return await call_next(request)          # the calendar feed is authenticated by its secret token
        if path.startswith("/static") or path in ("/login", "/signup", "/sw.js"):
            if user and path in ("/login", "/signup") and request.method == "GET":
                return RedirectResponse("/", 303)
            return await call_next(request)
        if not user:
            if path.startswith("/api/"):
                return JSONResponse({"status": "unauthenticated"}, status_code=401)
            return RedirectResponse("/login", 303)
        request.state.svc = service_for(user["id"])
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        return resp

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
        return set_session(RedirectResponse("/", 303), u["id"], request)

    @app.get("/signup")
    def signup_page(request: Request):
        return render(request, "login.html", mode="signup", settings=settings)

    @app.post("/signup")
    def signup(request: Request, name: str = Form(""), email: str = Form(""), password: str = Form(""), invite: str = Form("")):
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
        return set_session(RedirectResponse("/setup", 303), uid, request)

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
        try:
            request.state.svc.strava.disconnect()      # revoke on Strava's side too (best effort), freeing the athlete slot
        except Exception:  # noqa: BLE001
            log.exception("strava disconnect during account deletion failed")
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
        return render(request, "plan.html", weeks=svc.full_plan(), units=p.units if p else "km", today=svc.today().isoformat(),
                      has_profile=bool(p), outlook=svc.db.kv_get("volume_outlook"),
                      race_date=p.race_date.isoformat() if p else "")

    @app.get("/progress")
    def progress(request: Request):
        svc = request.state.svc
        p = svc.db.get_profile()
        return render(request, "progress.html", pr=svc.progress(), units=p.units if p else "km", runs=svc.recent_runs())

    @app.get("/run/{activity_id}")
    def run_page(request: Request, activity_id: int):
        svc = request.state.svc
        view = svc.run_detail(activity_id)
        if view is None:
            return back("/", err="That run wasn't found.")
        p = svc.db.get_profile()
        return render(request, "run.html", r=view, units=p.units if p else "km")

    # ---- crew ------------------------------------------------------------
    @app.get("/crew")
    def crew_page(request: Request):
        from . import crew as C
        uid, svc = request.state.user["id"], request.state.svc
        member = C.is_member(svc.db)
        return render(request, "crew.html", member=member, people=C.crew(db, uid, svc.today()), emoji=C.EMOJI)

    @app.post("/crew/join")
    def crew_join(request: Request):
        from . import crew as C
        C.join(request.state.svc.db)
        return back("/crew", "You're in the crew.")

    @app.post("/crew/leave")
    def crew_leave(request: Request):
        from . import crew as C
        C.leave(request.state.svc.db)
        return back("/me", "You left the crew. Others no longer see your stats.")

    @app.post("/crew/kudos/{to_user}")
    def crew_kudos(request: Request, to_user: int, emoji: str = Form(...)):
        from . import crew as C
        return do(lambda: C.send_kudos(db, request.state.user["id"], to_user, emoji, request.state.svc.today()), "/crew")

    @app.get("/sw.js")
    def service_worker():
        """Served from the root so it can control every page. Only registered by the browser on https/localhost."""
        return FileResponse(HERE / "static" / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/me")
    def me(request: Request):
        svc = request.state.svc
        p = svc.db.get_profile()
        import ipaddress

        base = str(request.base_url).rstrip("/")   # the address the user is browsing with, not a possibly-stale setting
        host = request.url.hostname or ""
        try:
            ipaddress.ip_address(host)
            is_ip = host not in ("127.0.0.1", "::1")
        except ValueError:
            is_ip = False
        d = svc.dashboard()
        used = len(db.strava_user_ids())
        full = d["strava_ready"] and not d["strava_connected"] and used >= settings.strava_athlete_limit
        tok = svc.calendar_token()
        feed = f"{base}/cal/{tok}.ics" if tok else None
        return render(request, "me.html", d=d, p=p, units=p.units if p else "km", settings=settings,
                      cal_url=feed, cal_webcal=("webcal://" + feed.split("://", 1)[1]) if feed else None,
                      callback_host=host, callback_is_ip=is_ip, callback_url=f"{base}/strava/callback",
                      strava_used=used, strava_full=full)

    @app.get("/setup")
    def setup(request: Request):
        from . import knowledge as K

        return render(request, "setup.html", p=request.state.svc.db.get_profile(), days=DAYS, presets=PRESETS, level_help=K.LEVEL_HELP, lengths=RUN_LENGTHS)

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
            raw_base = str(f.get("baseline", "")).strip()
            if raw_base == "":
                raise ValueError("tell us roughly how much you run in a typical week right now (enter 0 if you're just starting)")
            if float(raw_base) < 0:
                raise ValueError("weekly distance can't be negative")
            baseline_km = float(raw_base) * to_km
            p = Profile(
                name=request.state.user["name"], goal_label=label, goal_km=goal_km, race_date=date.fromisoformat(str(f.get("race_date"))),
                goal_time_s=goal_seconds(f), baseline_km=baseline_km,
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
        warn = svc.readiness_warnings()
        if warn:
            return back("/", err=f"Saved, but heads-up: {warn[0]}")
        return back("/", "Saved. Tap “Review & adjust” to re-plan around the changes." if had_plan else "Saved. One last step before your plan.")

    # ---- actions ---------------------------------------------------------
    @app.post("/plan/generate")
    def generate(request: Request):
        uid = request.state.user["id"]
        if request.state.svc.needs_strava_choice():
            msg = "Connect Strava first so your plan starts from your real runs, or choose “Skip for now”."
            if request.headers.get("x-requested-with"):
                return JSONResponse({"error": msg}, status_code=409)
            return back("/", err=msg)
        job = start_job(uid, "generate", "Your coach is building your plan", request.state.svc.generate_plan, "Your plan is ready.")
        return job_response(request, uid, job)

    @app.post("/review")
    def review(request: Request, note: str = Form("")):
        uid, svc, note = request.state.user["id"], request.state.svc, note.strip()
        job = start_job(uid, "review", "Your coach is reviewing your training", lambda: svc.review(note), "Plan reviewed and updated.")
        return job_response(request, uid, job)

    @app.post("/sync")
    def sync(request: Request):
        uid, svc = request.state.user["id"], request.state.svc
        job = start_job(uid, "sync", "Syncing with Strava", svc.sync, lambda n: f"Synced from Strava ({n} new run{'s' * (n != 1)}).")
        return job_response(request, uid, job, "/me")

    # ---- plan actions and run feedback (all deterministic) -----------------------------------------------
    def safe_next(v: str, default: str = "/") -> str:
        return v if v.startswith("/") and not v.startswith("//") and "\\" not in v and "?" not in v else default

    def do(fn, nxt: str, ok=None):
        try:
            res = fn()
            return back(nxt, ok(res) if callable(ok) else ok)
        except UserError as e:
            return back(nxt, err=str(e))

    @app.post("/workout/{wid}/skip")
    def workout_skip(request: Request, wid: int, reason: str = Form(""), next: str = Form("/")):
        return do(lambda: request.state.svc.skip_workout(wid, reason), safe_next(next), "Skipped. It won't count against your streak.")

    @app.post("/workout/{wid}/restore")
    def workout_restore(request: Request, wid: int, next: str = Form("/")):
        return do(lambda: request.state.svc.restore_workout(wid), safe_next(next), "Back on the plan.")

    @app.post("/workout/{wid}/move")
    def workout_move(request: Request, wid: int, to_date: str = Form(""), next: str = Form("/")):
        def go():
            try:
                to = date.fromisoformat(to_date)
            except ValueError:
                raise UserError("Pick a date.")
            what, warns = request.state.svc.reschedule(wid, to)
            return what + ("  Heads-up: " + " ".join(warns) if warns else "")
        return do(go, safe_next(next), lambda m: m)

    @app.post("/feedback")
    def feedback(request: Request, activity_id: int = Form(...), feeling: str = Form(""), rpe: str = Form(""), note: str = Form(""),
                 next: str = Form("/")):
        def go():
            return request.state.svc.save_feedback(activity_id, feeling, int(rpe) if rpe.strip().isdigit() else None, note)
        return do(go, safe_next(next), lambda r: ("Thanks for telling me. I've eased off the next week's hard sessions; see your coach note."
                                                  if r["pain"] else "Thanks, noted. Your coach will use this."))

    @app.post("/api/badges/seen")
    def badges_seen(request: Request):
        request.state.svc.mark_badges_seen()
        return JSONResponse({"ok": True})

    @app.post("/race-checklist")
    def race_checklist(request: Request, item: str = Form(""), checked: str = Form("1"), next: str = Form("/")):
        return do(lambda: request.state.svc.toggle_checklist(item, checked == "1"), safe_next(next))

    # ---- private calendar feed ------------------------------------------------------------------------------
    @app.get("/cal/{token}.ics")
    def calendar_feed(request: Request, token: str):
        import re as _re
        from datetime import datetime as _dt, timezone as _tz

        if not _re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token):
            return PlainTextResponse("Not found", 404)
        uid = db.user_id_by_kv("ics_token", token)
        if uid is None:
            return PlainTextResponse("Not found", 404)
        text = service_for(uid).calendar_text(str(request.base_url), _dt.now(_tz.utc))
        return PlainTextResponse(text, media_type="text/calendar; charset=utf-8",
                                 headers={"Cache-Control": "private, max-age=300", "Content-Disposition": 'inline; filename="running-coach.ics"'})

    @app.post("/calendar/enable")
    def calendar_enable(request: Request):
        request.state.svc.enable_calendar()
        return back("/me", "Calendar link created. Add it to your calendar below.")

    @app.post("/calendar/disable")
    def calendar_disable(request: Request):
        request.state.svc.disable_calendar()
        return back("/me", "Calendar link turned off. Anything subscribed to it will stop updating.")

    # ---- coach chat (cheapest model; proposals need a tap) --------------------------------------------------
    def chat_for(request: Request) -> ChatService:
        return ChatService(request.state.svc, chat_llm, settings.chat_daily_limit)

    @app.get("/coach")
    def coach_page(request: Request):
        cs = chat_for(request)
        p = request.state.svc.db.get_profile()
        return render(request, "chat.html", messages=request.state.svc.db.recent_chat(60), remaining=max(0, cs.limit - cs.used_today()),
                      limit=cs.limit, has_profile=bool(p))

    @app.post("/api/chat")
    async def api_chat(request: Request):
        try:
            body = await request.json()
            text = str(body.get("message", ""))
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Bad request."}, status_code=400)
        cs = chat_for(request)
        try:
            out = await asyncio.to_thread(cs.send, text)
            return JSONResponse(out, headers={"Cache-Control": "no-store"})
        except UserError as e:
            return JSONResponse({"error": str(e)}, status_code=429 if "chat messages" in str(e) else 400)
        except Exception as e:  # noqa: BLE001
            log.exception("chat failed")
            return JSONResponse({"error": explain(e) or f"Something went wrong: {e}"}, status_code=502)

    @app.post("/coach/send")
    def coach_send(request: Request, message: str = Form("")):
        try:
            chat_for(request).send(message)
            return RedirectResponse("/coach", 303)
        except UserError as e:
            return back("/coach", err=str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("chat failed")
            return back("/coach", err=explain(e) or f"Something went wrong: {e}")

    @app.post("/coach/proposal/{mid}/apply")
    def proposal_apply(request: Request, mid: int):
        try:
            res = chat_for(request).apply(mid)
        except UserError as e:
            return back("/coach", err=str(e))
        if res["replan"]:
            svc, uid, note = request.state.svc, request.state.user["id"], res["replan"]
            start_job(uid, "review", "Your coach is re-planning", lambda: svc.review(note), "Plan reviewed and updated.")
        msg = "Done: " + "; ".join(res["done"]) + "." + ("  Heads-up: " + " ".join(res["problems"]) if res["problems"] else "")
        return back("/coach", msg)

    @app.post("/coach/proposal/{mid}/dismiss")
    def proposal_dismiss(request: Request, mid: int):
        return do(lambda: chat_for(request).dismiss(mid), "/coach", "Dismissed.")

    @app.post("/coach/clear")
    def coach_clear(request: Request):
        request.state.svc.db.clear_chat()
        return back("/coach", "Chat cleared.")

    @app.get("/api/job")
    def api_job(request: Request):
        return JSONResponse(public_job(request.state.user["id"]), headers={"Cache-Control": "no-store"})

    # ---- strava oauth ----------------------------------------------------
    @app.get("/strava/connect")
    def strava_connect(request: Request):
        svc = request.state.svc
        if not getattr(svc.strava, "configured", False):
            return back("/me", err="Strava isn't set up on this server yet. Ask whoever runs it to add the Strava app.")
        if not svc.strava.connected:
            used = len(db.strava_user_ids())
            if used >= settings.strava_athlete_limit:
                return back("/me", err=f"This server's Strava app is full ({used} of {settings.strava_athlete_limit} athletes connected). "
                                       "Ask the person who runs it to free a slot or raise the limit.")
        state = secrets.token_urlsafe(16)
        svc.db.kv_set("strava_state", state)
        return RedirectResponse(svc.strava.authorize_url(state, str(request.base_url)))

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
        msg = f"Strava connected; imported {n} recent runs."
        return back("/" if svc.db.get_profile() and not svc.db.get_workouts() else "/me", msg)

    @app.post("/strava/skip")
    def strava_skip(request: Request):
        request.state.svc.db.kv_set("strava_skipped", True)
        return back("/", "No problem. You can connect Strava any time from the Me tab; your plan will start from the weekly distance you entered.")

    @app.post("/strava/disconnect")
    def strava_disconnect(request: Request):
        request.state.svc.strava.disconnect()
        return back("/me", "Strava disconnected.")

    return app
