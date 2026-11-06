"""A private calendar feed (iCalendar, RFC 5545) of the athlete's plan, so workouts and alerts appear in Apple/Google Calendar.

Times are *floating* (no time zone) so a 6:30 run is 6:30 wherever the phone is. Workouts with no usual time become all-day
events. Skipped workouts are left out, so calendars drop them on the next refresh."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .knowledge import KM_PER_MI

TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*([ap]m)?\s*$", re.I)
EMOJI = {"easy": "🏃", "long": "🏃", "tempo": "⚡", "intervals": "🔥", "recovery": "🚶", "race": "🏁", "cross": "🚴"}
ALARM_BEFORE_MIN = 30      # timed workouts: alert this long before
ALARM_ALLDAY_HOUR = 7      # all-day workouts: alert at 07:00


def parse_time(text: str) -> tuple[int, int] | None:
    """'06:30', '6:30am', '6:30 PM', '18:05' -> (h, m); anything else -> None."""
    m = TIME_RE.match(text or "")
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if ap:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ap == "pm" else 0)
    return (h, mi) if h < 24 and mi < 60 else None


def escape_text(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n"))


def fold(line: str) -> str:
    """Fold to ≤75 octets per physical line (continuations start with one space); never splits a UTF-8 character."""
    out, cur, size, limit = [], "", 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            out.append(cur)
            cur, size, limit = " ", 1, 75
        cur += ch
        size += n
    out.append(cur)
    return "\r\n".join(out)


def _dt(d: datetime) -> str:
    return d.strftime("%Y%m%dT%H%M%S")


def build_calendar(workouts: list[dict], *, availability: dict, units: str, user_id: int, base_url: str, now: datetime,
                   name: str = "RunningTime", default_minutes: int = 45) -> str:
    host = re.sub(r"^https?://", "", base_url).split("/")[0] or "localhost"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//RunningTime//Plan//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{escape_text(name)}", "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    day_keys = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    for w in sorted(workouts, key=lambda w: w["date"]):
        if w["status"] == "skipped":
            continue
        d = date.fromisoformat(w["date"])
        km = w.get("distance_km") or 0.0
        dist = f"{km / KM_PER_MI:.1f} mi" if units == "mi" else f"{km:.1f} km"
        prefix = "✅ " if w["status"] in ("completed", "partial") else ""
        title = f"{prefix}{EMOJI.get(w['type'], '🏃')} {w['title']}" + (f" ({dist})" if km else "")
        desc = [w.get("description") or ""]
        if w.get("pace_guidance"):
            desc.append(f"Pace: {w['pace_guidance']}")
        if w.get("duration_min"):
            desc.append(f"About {w['duration_min']} min.")
        desc.append(f"Open RunningTime: {base_url.rstrip('/')}/")
        t = parse_time((availability.get(day_keys[d.weekday()]) or {}).get("time", ""))
        ev = ["BEGIN:VEVENT", f"UID:rc-{user_id}-w{w['id']}@{host}", f"DTSTAMP:{now.strftime('%Y%m%dT%H%M%SZ')}"]
        if t:
            start = datetime(d.year, d.month, d.day, t[0], t[1])
            end = start + timedelta(minutes=w.get("duration_min") or default_minutes)
            ev += [f"DTSTART:{_dt(start)}", f"DTEND:{_dt(end)}"]
            trigger = f"-PT{ALARM_BEFORE_MIN}M"
        else:
            ev += [f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}", f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}"]
            trigger = f"PT{ALARM_ALLDAY_HOUR}H"
        ev += [f"SUMMARY:{escape_text(title)}", f"DESCRIPTION:{escape_text(chr(10).join(x for x in desc if x))}",
               "TRANSP:TRANSPARENT",                      # a run shouldn't mark you "busy" on shared calendars
               "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{escape_text(w['title'])}", f"TRIGGER;RELATED=START:{trigger}", "END:VALARM",
               "END:VEVENT"]
        lines += ev
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"
