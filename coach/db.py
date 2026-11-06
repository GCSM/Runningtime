from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from .models import Profile

SCHEMA = """
create table if not exists users(
    id integer primary key autoincrement, email text not null unique collate nocase,
    name text not null default '', pw_hash text not null, created_at text not null);
create table if not exists sessions(
    token_hash text primary key, user_id integer not null references users(id) on delete cascade, expires_at text not null);
create table if not exists kv(
    user_id integer not null, key text not null, value text not null, primary key(user_id, key));
create table if not exists profile(user_id integer primary key, data text not null);
create table if not exists weeks(
    user_id integer not null, week_start text not null, phase text not null, recovery integer not null,
    race_week integer not null, target_km real not null, long_run_km real not null, primary key(user_id, week_start));
create table if not exists workouts(
    id integer primary key autoincrement, user_id integer not null,
    date text not null, week_start text not null, type text not null,
    title text not null, description text not null default '',
    distance_km real, duration_min integer, pace_guidance text not null default '',
    status text not null default 'planned', actual_km real, activity_id integer);
create index if not exists workouts_user_date on workouts(user_id, date);
create table if not exists activities(
    user_id integer not null, id integer not null, start_local text not null, date text not null, name text,
    distance_m real not null, moving_s integer not null, avg_hr real, elevation_m real, sport text,
    primary key(user_id, id));
create index if not exists activities_user_date on activities(user_id, date);
create table if not exists coach_log(
    id integer primary key autoincrement, user_id integer not null,
    created_at text not null, kind text not null, message text not null);
"""

USER_TABLES = ("kv", "profile", "weeks", "workouts", "activities", "coach_log")


class DB:
    """SQLite store. The unbound instance handles accounts/sessions; `for_user(uid)` returns a view
    whose every query is scoped to that user."""

    def __init__(self, path: str, uid: int = 0, _init: bool = True):
        self.path, self.uid = path, uid
        if _init:
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            with self.conn() as c:
                c.executescript(SCHEMA)

    def for_user(self, uid: int) -> "DB":
        b = copy.copy(self)
        b.uid = uid
        return b

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("pragma foreign_keys=on")
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    # ---- accounts (unbound) ------------------------------------------------
    def create_user(self, email: str, name: str, pw_hash: str) -> int:
        with self.conn() as c:
            cur = c.execute(
                "insert into users(email,name,pw_hash,created_at) values(?,?,?,?)",
                (email.strip(), name.strip(), pw_hash, _now()),
            )
            return cur.lastrowid

    def user_by_email(self, email: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("select * from users where email=?", (email.strip(),)).fetchone()
        return dict(r) if r else None

    def user_by_id(self, uid: int) -> dict | None:
        with self.conn() as c:
            r = c.execute("select * from users where id=?", (uid,)).fetchone()
        return dict(r) if r else None

    def create_session(self, token_hash: str, uid: int, expires_at: str) -> None:
        with self.conn() as c:
            c.execute("delete from sessions where expires_at<?", (_now(),))
            c.execute("insert into sessions values(?,?,?)", (token_hash, uid, expires_at))

    def session_user(self, token_hash: str) -> dict | None:
        with self.conn() as c:
            r = c.execute(
                "select u.* from sessions s join users u on u.id=s.user_id where s.token_hash=? and s.expires_at>?",
                (token_hash, _now()),
            ).fetchone()
        return dict(r) if r else None

    def delete_session(self, token_hash: str) -> None:
        with self.conn() as c:
            c.execute("delete from sessions where token_hash=?", (token_hash,))

    def delete_user(self, uid: int) -> None:
        with self.conn() as c:
            for t in USER_TABLES:
                c.execute(f"delete from {t} where user_id=?", (uid,))
            c.execute("delete from users where id=?", (uid,))  # sessions cascade

    def strava_user_ids(self) -> list[int]:
        with self.conn() as c:
            return [r["user_id"] for r in c.execute("select user_id from kv where key='strava_token'")]

    # ---- key/value -------------------------------------------------------
    def kv_get(self, key: str, default=None):
        with self.conn() as c:
            r = c.execute("select value from kv where user_id=? and key=?", (self.uid, key)).fetchone()
        return json.loads(r["value"]) if r else default

    def kv_set(self, key: str, value) -> None:
        with self.conn() as c:
            c.execute(
                "insert into kv(user_id,key,value) values(?,?,?) on conflict(user_id,key) do update set value=excluded.value",
                (self.uid, key, json.dumps(value)),
            )

    def kv_delete(self, key: str) -> None:
        with self.conn() as c:
            c.execute("delete from kv where user_id=? and key=?", (self.uid, key))

    # ---- profile ---------------------------------------------------------
    def get_profile(self) -> Profile | None:
        with self.conn() as c:
            r = c.execute("select data from profile where user_id=?", (self.uid,)).fetchone()
        return Profile.model_validate_json(r["data"]) if r else None

    def save_profile(self, p: Profile) -> None:
        with self.conn() as c:
            c.execute(
                "insert into profile(user_id,data) values(?,?) on conflict(user_id) do update set data=excluded.data",
                (self.uid, p.model_dump_json()),
            )

    # ---- activities ------------------------------------------------------
    def upsert_activities(self, acts: list[dict]) -> int:
        n = 0
        with self.conn() as c:
            for a in acts:
                exists = c.execute("select 1 from activities where user_id=? and id=?", (self.uid, a["id"])).fetchone()
                c.execute(
                    """insert into activities(user_id,id,start_local,date,name,distance_m,moving_s,avg_hr,elevation_m,sport)
                       values(:uid,:id,:start_local,:date,:name,:distance_m,:moving_s,:avg_hr,:elevation_m,:sport)
                       on conflict(user_id,id) do update set start_local=excluded.start_local, date=excluded.date,
                       name=excluded.name, distance_m=excluded.distance_m, moving_s=excluded.moving_s,
                       avg_hr=excluded.avg_hr, elevation_m=excluded.elevation_m, sport=excluded.sport""",
                    {**a, "uid": self.uid},
                )
                n += 0 if exists else 1
        return n

    def activities_since(self, since: date) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "select * from activities where user_id=? and date>=? order by start_local", (self.uid, since.isoformat())
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_activity_date(self) -> date | None:
        with self.conn() as c:
            r = c.execute("select max(date) d from activities where user_id=?", (self.uid,)).fetchone()
        return date.fromisoformat(r["d"]) if r and r["d"] else None

    # ---- plan ------------------------------------------------------------
    def replace_plan_from(self, from_date: date, weeks: list[dict], workouts: list[dict]) -> None:
        """Replace all not-yet-done workouts on/after from_date, and week targets from that week on."""
        from .skeleton import monday

        wk = monday(from_date).isoformat()
        with self.conn() as c:
            c.execute("delete from workouts where user_id=? and date>=? and status='planned'", (self.uid, from_date.isoformat()))
            c.execute("delete from weeks where user_id=? and week_start>=?", (self.uid, wk))
            for w in weeks:
                c.execute(
                    "insert into weeks values(:uid,:week_start,:phase,:recovery,:race_week,:target_km,:long_run_km)",
                    {**w, "uid": self.uid},
                )
            for w in workouts:
                c.execute(
                    """insert into workouts(user_id,date,week_start,type,title,description,distance_km,duration_min,pace_guidance)
                       values(:uid,:date,:week_start,:type,:title,:description,:distance_km,:duration_min,:pace_guidance)""",
                    {**w, "uid": self.uid},
                )

    def get_workouts(self, start: date | None = None, end: date | None = None) -> list[dict]:
        q, args = "select * from workouts where user_id=?", [self.uid]
        if start:
            q += " and date>=?"
            args.append(start.isoformat())
        if end:
            q += " and date<=?"
            args.append(end.isoformat())
        with self.conn() as c:
            return [dict(r) for r in c.execute(q + " order by date", args).fetchall()]

    def get_weeks(self) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("select * from weeks where user_id=? order by week_start", (self.uid,))]

    def update_workout_status(self, wid: int, status: str, actual_km: float | None, activity_id: int | None) -> None:
        with self.conn() as c:
            c.execute(
                "update workouts set status=?, actual_km=?, activity_id=? where id=? and user_id=?",
                (status, actual_km, activity_id, wid, self.uid),
            )

    # ---- coach log -------------------------------------------------------
    def log(self, kind: str, message: str) -> None:
        with self.conn() as c:
            c.execute(
                "insert into coach_log(user_id,created_at,kind,message) values(?,?,?,?)", (self.uid, _now(), kind, message)
            )

    def recent_log(self, limit: int = 10) -> list[dict]:
        with self.conn() as c:
            return [
                dict(r)
                for r in c.execute("select * from coach_log where user_id=? order by id desc limit ?", (self.uid, limit))
            ]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
