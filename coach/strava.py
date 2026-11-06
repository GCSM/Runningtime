from __future__ import annotations

import time
from urllib.parse import urlencode

import httpx

from .config import Settings
from .db import DB

AUTH_URL = "https://www.strava.com/oauth/authorize"
API = "https://www.strava.com/api/v3"
RUN_SPORTS = {"Run", "TrailRun", "VirtualRun"}


class StravaError(RuntimeError):
    pass


def normalize(a: dict) -> dict | None:
    sport = a.get("sport_type") or a.get("type")
    if sport not in RUN_SPORTS or not a.get("distance"):
        return None
    local = a.get("start_date_local") or a["start_date"]
    return {
        "id": a["id"], "start_local": local, "date": local[:10], "name": a.get("name"),
        "distance_m": float(a["distance"]), "moving_s": int(a.get("moving_time") or 0),
        "avg_hr": a.get("average_heartrate"), "elevation_m": a.get("total_elevation_gain"), "sport": sport,
    }


class StravaClient:
    def __init__(self, settings: Settings, db: DB):
        self.s, self.db = settings, db

    # ---- app credentials: each user's own Strava app, falling back to a server-wide one --------------
    def _creds(self) -> tuple[str, str]:
        app = self.db.kv_get("strava_app") or {}
        return (app.get("client_id") or self.s.strava_client_id, app.get("client_secret") or self.s.strava_client_secret)

    @property
    def configured(self) -> bool:
        cid, sec = self._creds()
        return bool(cid and sec)

    @property
    def app_source(self) -> str:
        """'user' if this account saved its own app, 'server' if falling back to the shared one, else ''."""
        if self.db.kv_get("strava_app"):
            return "user"
        return "server" if self.s.strava_configured else ""

    def app_client_id(self) -> str:
        return self._creds()[0]

    def save_app(self, client_id: str, client_secret: str) -> None:
        old = self.db.kv_get("strava_app") or {}
        self.db.kv_set("strava_app", {"client_id": client_id, "client_secret": client_secret})
        if old.get("client_id") != client_id:  # tokens belong to the app that issued them
            self.db.kv_delete("strava_token")

    def remove_app(self) -> None:
        self.db.kv_delete("strava_app")
        self.db.kv_delete("strava_token")

    @property
    def connected(self) -> bool:
        return bool(self.db.kv_get("strava_token"))

    def disconnect(self) -> None:
        self.db.kv_delete("strava_token")

    def athlete_name(self) -> str:
        return (self.db.kv_get("strava_token") or {}).get("athlete_name", "")

    def authorize_url(self, state: str, base_url: str | None = None) -> str:
        """base_url: the address the user is browsing with (so the callback can never point at a stale domain)."""
        q = {
            "client_id": self._creds()[0], "response_type": "code", "approval_prompt": "auto",
            "redirect_uri": f"{(base_url or self.s.public_url).rstrip('/')}/strava/callback", "scope": "read,activity:read_all", "state": state,
        }
        return f"{AUTH_URL}?{urlencode(q)}"

    def _token_request(self, **data) -> dict:
        cid, sec = self._creds()
        r = httpx.post(
            f"{API}/oauth/token",
            data={"client_id": cid, "client_secret": sec, **data},
            timeout=20,
        )
        if r.status_code != 200:
            raise StravaError(f"Strava token request failed: {r.status_code} {r.text[:200]}")
        return r.json()

    def exchange_code(self, code: str) -> None:
        t = self._token_request(code=code, grant_type="authorization_code")
        ath = t.get("athlete") or {}
        self.db.kv_set("strava_token", {
            "access_token": t["access_token"], "refresh_token": t["refresh_token"], "expires_at": t["expires_at"],
            "athlete_name": f"{ath.get('firstname', '')} {ath.get('lastname', '')}".strip(),
        })

    def _access_token(self) -> str:
        tok = self.db.kv_get("strava_token")
        if not tok:
            raise StravaError("Strava is not connected")
        if tok["expires_at"] - 60 < time.time():
            t = self._token_request(grant_type="refresh_token", refresh_token=tok["refresh_token"])
            tok.update(access_token=t["access_token"], refresh_token=t["refresh_token"], expires_at=t["expires_at"])
            self.db.kv_set("strava_token", tok)
        return tok["access_token"]

    def fetch_runs(self, after_ts: int) -> list[dict]:
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        out: list[dict] = []
        page = 1
        while True:
            r = httpx.get(f"{API}/athlete/activities", headers=headers, timeout=30,
                          params={"after": after_ts, "per_page": 100, "page": page})
            if r.status_code != 200:
                raise StravaError(f"Strava activities request failed: {r.status_code} {r.text[:200]}")
            batch = r.json()
            out += [n for a in batch if (n := normalize(a))]
            if len(batch) < 100:
                return out
            page += 1
