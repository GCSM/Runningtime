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

    @property
    def connected(self) -> bool:
        return bool(self.db.kv_get("strava_token"))

    def disconnect(self) -> None:
        self.db.kv_delete("strava_token")

    def athlete_name(self) -> str:
        return (self.db.kv_get("strava_token") or {}).get("athlete_name", "")

    def authorize_url(self, state: str) -> str:
        q = {
            "client_id": self.s.strava_client_id, "response_type": "code", "approval_prompt": "auto",
            "redirect_uri": f"{self.s.public_url}/strava/callback", "scope": "read,activity:read_all", "state": state,
        }
        return f"{AUTH_URL}?{urlencode(q)}"

    def _token_request(self, **data) -> dict:
        r = httpx.post(
            f"{API}/oauth/token",
            data={"client_id": self.s.strava_client_id, "client_secret": self.s.strava_client_secret, **data},
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
