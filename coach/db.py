from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from .models import Profile

SCHEMA = """
create table if not exists kv(key text primary key, value text not null);
create table if not exists profile(id integer primary key check(id=1), data text not null);
create table if not exists weeks(
    week_start text primary key, phase text not null, recovery integer not null,
    race_week integer not null, target_km real not null, long_run_km real not null);
create table if not exists workouts(
    id integer primary key autoincrement,
    date text not null, week_start text not null, type text not null,
    title text not null, description text not null default '',
    distance_km real, duration_min integer, pace_guidance text not null default '',
    status text not null default 'planned', actual_km real, activity_id integer);
create index if not exists workouts_date on workouts(date);
create table if not exists activities(
    id integer primary key, start_local text not null, date text not null, name text,
    distance_m real not null, moving_s integer not null, avg_hr real, elevation_m real, sport text);
create index if not exists activities_date on activities(date);
create table if not exists coach_log(
    id integer primary key autoincrement, created_at text not null, kind text not null, message text not null);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    # ---- key/value -------------------------------------------------------
    def kv_get(self, key: str, default=None):
        with self.conn() as c:
            r = c.execute("select value from kv where key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def kv_set(self, key: str, value) -> None:
        with self.conn() as c:
            c.execute(
                "insert into kv(key,value) values(?,?) on conflict(key) do update set value=excluded.value",
                (key, json.dumps(value)),
            )

    # ---- profile ---------------------------------------------------------
    def get_profile(self) -> Profile | None:
        with self.conn() as c:
            r = c.execute("select data from profile where id=1").fetchone()
        return Profile.model_validate_json(r["data"]) if r else None

    def save_profile(self, p: Profile) -> None:
        with self.conn() as c:
            c.execute(
                "insert into profile(id,data) values(1,?) on conflict(id) do update set data=excluded.data",
                (p.model_dump_json(),),
            )

    # ---- activities ------------------------------------------------------
    def upsert_activities(self, acts: list[dict]) -> int:
        n = 0
        with self.conn() as c:
            for a in acts:
                exists = c.execute("select 1 from activities where id=?", (a["id"],)).fetchone()
                c.execute(
                    """insert into activities(id,start_local,date,name,distance_m,moving_s,avg_hr,elevation_m,sport)
                       values(:id,:start_local,:date,:name,:distance_m,:moving_s,:avg_hr,:elevation_m,:sport)
                       on conflict(id) do update set start_local=excluded.start_local, date=excluded.date,
                       name=excluded.name, distance_m=excluded.distance_m, moving_s=excluded.moving_s,
                       avg_hr=excluded.avg_hr, elevation_m=excluded.elevation_m, sport=excluded.sport""",
                    a,
                )
                n += 0 if exists else 1
        return n

    def activities_since(self, since: date) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "select * from activities where date>=? order by start_local", (since.isoformat(),)
            ).fetchall()
        return [dict(r) for r in rows]

    def has_activities(self) -> bool:
        with self.conn() as c:
            return c.execute("select 1 from activities limit 1").fetchone() is not None

    def latest_activity_date(self) -> date | None:
        with self.conn() as c:
            r = c.execute("select max(date) d from activities").fetchone()
        return date.fromisoformat(r["d"]) if r and r["d"] else None

    # ---- plan ------------------------------------------------------------
    def replace_plan_from(self, from_date: date, weeks: list[dict], workouts: list[dict]) -> None:
        """Replace all not-yet-done workouts on/after from_date, and week targets from that week on."""
        from .skeleton import monday

        wk = monday(from_date).isoformat()
        with self.conn() as c:
            c.execute("delete from workouts where date>=? and status='planned'", (from_date.isoformat(),))
            c.execute("delete from weeks where week_start>=?", (wk,))
            for w in weeks:
                c.execute(
                    "insert into weeks values(:week_start,:phase,:recovery,:race_week,:target_km,:long_run_km)", w
                )
            for w in workouts:
                c.execute(
                    """insert into workouts(date,week_start,type,title,description,distance_km,duration_min,pace_guidance)
                       values(:date,:week_start,:type,:title,:description,:distance_km,:duration_min,:pace_guidance)""",
                    w,
                )

    def get_workouts(self, start: date | None = None, end: date | None = None) -> list[dict]:
        q, args = "select * from workouts", []
        cond = []
        if start:
            cond.append("date>=?")
            args.append(start.isoformat())
        if end:
            cond.append("date<=?")
            args.append(end.isoformat())
        if cond:
            q += " where " + " and ".join(cond)
        with self.conn() as c:
            return [dict(r) for r in c.execute(q + " order by date", args).fetchall()]

    def get_weeks(self) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("select * from weeks order by week_start").fetchall()]

    def update_workout_status(self, wid: int, status: str, actual_km: float | None, activity_id: int | None) -> None:
        with self.conn() as c:
            c.execute(
                "update workouts set status=?, actual_km=?, activity_id=? where id=?",
                (status, actual_km, activity_id, wid),
            )

    # ---- coach log -------------------------------------------------------
    def log(self, kind: str, message: str) -> None:
        with self.conn() as c:
            c.execute(
                "insert into coach_log(created_at,kind,message) values(?,?,?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), kind, message),
            )

    def recent_log(self, limit: int = 10) -> list[dict]:
        with self.conn() as c:
            return [
                dict(r)
                for r in c.execute("select * from coach_log order by id desc limit ?", (limit,)).fetchall()
            ]
