"""Strava integration: one server-wide Strava app, one token set per user.

The operator creates a single Strava app (STRAVA_CLIENT_ID/SECRET). Each user just taps "Connect with Strava" and we
store *their* tokens. Failures are turned into messages a person can act on: revoked access, rate limits, no network.
"""
from __future__ import annotations

import time
from urllib.parse import urlencode

import httpx

from .config import Settings
from .db import DB

AUTH_URL = "https://www.strava.com/oauth/authorize"
DEAUTH_URL = "https://www.strava.com/oauth/deauthorize"
API = "https://www.strava.com/api/v3"
RUN_SPORTS = {"Run", "TrailRun", "VirtualRun"}

MSG_REVOKED = "Strava access was removed or has expired. Tap “Connect with Strava” on the Me tab to reconnect."
MSG_RATE = "Strava's rate limit was reached. Your runs will sync again automatically a little later."
MSG_NETWORK = "Couldn't reach Strava. Check that the server has internet access, then try again."
MSG_NOT_CONFIGURED = "Strava isn't set up on this server yet."


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


DETAIL_KEYS = ("name", "description", "distance", "moving_time", "elapsed_time", "total_elevation_gain", "average_heartrate",
               "max_heartrate", "average_speed", "start_date_local", "sport_type", "type", "device_name", "calories")


def trim_activity_detail(a: dict) -> dict:
    """Keep only what the run page shows. Strava's full detail has much more (photos, segment efforts, ...) that we don't store."""
    out = {k: a[k] for k in DETAIL_KEYS if a.get(k) is not None}
    m = a.get("map") or {}
    out["polyline"] = m.get("polyline") or m.get("summary_polyline") or ""
    out["splits"] = [
        {k: sp.get(k) for k in ("split", "distance", "moving_time", "elapsed_time", "average_heartrate", "elevation_difference")}
        for sp in (a.get("splits_metric") or [])
    ]
    return out


class StravaClient:
    def __init__(self, settings: Settings, db: DB):
        self.s, self.db = settings, db   # db is bound to one user

    # ---- state -------------------------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return self.s.strava_configured

    @property
    def connected(self) -> bool:
        return bool(self.db.kv_get("strava_token"))

    def athlete_name(self) -> str:
        return (self.db.kv_get("strava_token") or {}).get("athlete_name", "")

    def _forget(self) -> None:
        """Drop this user's Strava connection (and any leftover per-user app from an older version)."""
        self.db.kv_delete("strava_token")
        self.db.kv_delete("strava_app")

    # ---- OAuth -------------------------------------------------------------------------------------
    def authorize_url(self, state: str, base_url: str | None = None) -> str:
        """base_url: the address the user is browsing with, so the callback can never point at a stale domain."""
        q = {
            "client_id": self.s.strava_client_id, "response_type": "code", "approval_prompt": "auto",
            "redirect_uri": f"{(base_url or self.s.public_url).rstrip('/')}/strava/callback",
            "scope": "read,activity:read_all", "state": state,
        }
        return f"{AUTH_URL}?{urlencode(q)}"

    def _token_request(self, **data) -> dict:
        try:
            r = httpx.post(
                f"{API}/oauth/token",
                data={"client_id": self.s.strava_client_id, "client_secret": self.s.strava_client_secret, **data},
                timeout=20,
            )
        except httpx.HTTPError as e:
            raise StravaError(MSG_NETWORK) from e
        if r.status_code == 429:
            raise StravaError(MSG_RATE)
        if r.status_code != 200:
            raise StravaError(f"Strava refused the request ({r.status_code}): {r.text[:200]}")
        return r.json()

    def exchange_code(self, code: str) -> None:
        t = self._token_request(code=code, grant_type="authorization_code")
        ath = t.get("athlete") or {}
        self.db.kv_set("strava_token", {
            "access_token": t["access_token"], "refresh_token": t["refresh_token"], "expires_at": t["expires_at"],
            "athlete_name": f"{ath.get('firstname', '')} {ath.get('lastname', '')}".strip(),
        })
        self.db.kv_delete("strava_app")

    def _access_token(self, force_refresh: bool = False) -> str:
        tok = self.db.kv_get("strava_token")
        if not tok:
            raise StravaError("Strava is not connected")
        if force_refresh or tok["expires_at"] - 60 < time.time():
            try:
                t = self._token_request(grant_type="refresh_token", refresh_token=tok["refresh_token"])
            except StravaError as e:
                if str(e).startswith("Strava refused the request (4"):   # refresh token revoked / issued by a different app
                    self._forget()
                    raise StravaError(MSG_REVOKED) from e
                raise
            tok.update(access_token=t["access_token"], refresh_token=t["refresh_token"], expires_at=t["expires_at"])
            self.db.kv_set("strava_token", tok)
        return tok["access_token"]

    # ---- data --------------------------------------------------------------------------------------
    def fetch_runs(self, after_ts: int) -> list[dict]:
        token = self._access_token()
        out: list[dict] = []
        page, refreshed = 1, False
        while True:
            try:
                r = httpx.get(f"{API}/athlete/activities", headers={"Authorization": f"Bearer {token}"}, timeout=30,
                              params={"after": after_ts, "per_page": 100, "page": page})
            except httpx.HTTPError as e:
                raise StravaError(MSG_NETWORK) from e
            if r.status_code == 401 and not refreshed:      # token rejected: try a refresh once, then treat as revoked
                token, refreshed = self._access_token(force_refresh=True), True
                continue
            if r.status_code == 401:
                self._forget()
                raise StravaError(MSG_REVOKED)
            if r.status_code == 429:
                raise StravaError(MSG_RATE)
            if r.status_code != 200:
                raise StravaError(f"Strava activities request failed ({r.status_code}): {r.text[:200]}")
            batch = r.json()
            out += [n for a in batch if (n := normalize(a))]
            if len(batch) < 100:
                return out
            page += 1

    def fetch_activity(self, activity_id: int) -> dict:
        """One activity's detail (route, kilometre splits, heart rate), trimmed to what we display."""
        token, refreshed = self._access_token(), False
        while True:
            try:
                r = httpx.get(f"{API}/activities/{int(activity_id)}", headers={"Authorization": f"Bearer {token}"}, timeout=30)
            except httpx.HTTPError as e:
                raise StravaError(MSG_NETWORK) from e
            if r.status_code == 401 and not refreshed:
                token, refreshed = self._access_token(force_refresh=True), True
                continue
            if r.status_code == 401:
                self._forget()
                raise StravaError(MSG_REVOKED)
            if r.status_code == 429:
                raise StravaError(MSG_RATE)
            if r.status_code in (403, 404):
                raise StravaError("Strava has no details for this run any more (it may have been deleted or made private).")
            if r.status_code != 200:
                raise StravaError(f"Strava activity request failed ({r.status_code}): {r.text[:200]}")
            return trim_activity_detail(r.json())

    # ---- disconnect --------------------------------------------------------------------------------
    def disconnect(self) -> None:
        """Revoke our access on Strava's side (best effort), then forget the tokens locally."""
        if self.connected:
            try:
                token = self._access_token()
                httpx.post(DEAUTH_URL, data={"access_token": token}, headers={"Authorization": f"Bearer {token}"}, timeout=15)
            except Exception:  # noqa: BLE001 - never block the user from disconnecting
                pass
        self._forget()
