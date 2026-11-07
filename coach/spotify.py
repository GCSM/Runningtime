"""Spotify: connect a user's account (Authorization Code + PKCE, no client secret) and create playlists in it.

Spotify only accepts https redirect URLs or a loopback address (http://127.0.0.1...). A home server on plain HTTP can't be a redirect
target, so the redirect is a loopback address that deliberately goes nowhere on the phone: after approving, the user copies the address
shown in the browser and pastes it into the app (`parse_pasted`). The one-time code in it is useless without the PKCE verifier, which
never leaves the server.

Developer-mode apps are limited (owner needs Premium, 5 authorized users, endpoints renamed in Feb 2026), so every failure is turned
into a message that says what to check. Written from Spotify's published docs and developer reports; not run against the live service.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import Settings
from .db import DB

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = "playlist-modify-private"
PKCE_TTL_S = 15 * 60

MSG_NETWORK = "Couldn't reach Spotify. Check that the server has internet access, then try again."
MSG_RATE = "Spotify is rate-limiting requests. Wait a minute and try again."
MSG_RECONNECT = "Your Spotify connection expired or was removed. Connect Spotify again to continue."
MSG_FORBIDDEN = ("Spotify refused the request (403). In developer mode this usually means your Spotify account isn't on the app's user list "
                 "(Spotify developer dashboard → your app → Settings → User Management) or the app owner doesn't have Premium. "
                 "Spotify has also been reported to block playlist creation for some developer-mode apps.")
MSG_NOT_CONFIGURED = "Spotify isn't set up on this server yet."


class SpotifyError(RuntimeError):
    pass


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge) per RFC 7636."""
    verifier = _b64url(secrets.token_bytes(64))
    return verifier, _b64url(hashlib.sha256(verifier.encode()).digest())


def parse_pasted(text: str) -> tuple[str, str]:
    """Pull (code, state) out of whatever the user pasted: the full redirect address, just its query string, or a bare code.

    Raises SpotifyError with a plain message if Spotify reported an error (e.g. the user tapped Cancel) or nothing usable was found.
    """
    t = (text or "").strip().strip("<>\"'")
    if not t:
        raise SpotifyError("Paste the address from the page Spotify sent you to.")
    if "code=" in t or "error=" in t:
        q = parse_qs(urlparse(t).query if "?" in t else t.lstrip("?#"))
        if q.get("error"):
            raise SpotifyError("Spotify access was cancelled." if q["error"][0] == "access_denied" else f"Spotify said: {q['error'][0]}.")
        code = (q.get("code") or [""])[0]
        if code:
            return code, (q.get("state") or [""])[0]
    if re.fullmatch(r"[A-Za-z0-9_\-]{20,600}", t):
        return t, ""
    raise SpotifyError("That doesn't look like the address Spotify sent you to. It should contain “code=”. Copy the whole address from the browser.")


def _norm(s: str) -> str:
    s = re.sub(r"\(.*?\)|\[.*?\]|feat\.?.*|ft\.?.*|remaster(ed)?.*|- .*(version|mix|edit|remaster).*", "", s.lower())
    return re.sub(r"[^a-z0-9]+", "", s)


class SpotifyClient:
    def __init__(self, settings: Settings, db: DB, http=None):
        self.s, self.db, self.http = settings, db, http or httpx      # db is bound to one user

    # ---- state ------------------------------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.s.spotify_client_id)

    @property
    def connected(self) -> bool:
        return bool(self.db.kv_get("spotify_token"))

    def account_name(self) -> str:
        return (self.db.kv_get("spotify_token") or {}).get("name", "")

    def _forget(self) -> None:
        self.db.kv_delete("spotify_token")

    # ---- connect ----------------------------------------------------------------------------------------
    def begin(self) -> str:
        """Start a connection: remember the PKCE secret and state for this user, return the Spotify authorize URL."""
        if not self.configured:
            raise SpotifyError(MSG_NOT_CONFIGURED)
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(16)
        self.db.kv_set("spotify_pkce", {"verifier": verifier, "state": state, "at": time.time()})
        q = {"client_id": self.s.spotify_client_id, "response_type": "code", "redirect_uri": self.s.spotify_redirect_uri, "scope": SCOPES,
             "state": state, "code_challenge_method": "S256", "code_challenge": challenge, "show_dialog": "false"}
        return f"{AUTH_URL}?{urlencode(q)}"

    def _token_request(self, **data) -> dict:
        try:
            r = self.http.post(TOKEN_URL, data={"client_id": self.s.spotify_client_id, **data}, timeout=20)
        except httpx.HTTPError as e:
            raise SpotifyError(MSG_NETWORK) from e
        if r.status_code == 429:
            raise SpotifyError(MSG_RATE)
        if r.status_code != 200:
            try:
                err = r.json().get("error_description") or r.json().get("error") or ""
            except Exception:  # noqa: BLE001
                err = ""
            raise SpotifyError(f"Spotify refused the login ({r.status_code}){': ' + str(err)[:160] if err else ''}")
        return r.json()

    def finish(self, pasted: str) -> str:
        """Complete the connection from the pasted address. Returns the account's display name."""
        code, state = parse_pasted(pasted)
        pk = self.db.kv_get("spotify_pkce")
        if not pk or time.time() - pk.get("at", 0) > PKCE_TTL_S:
            self.db.kv_delete("spotify_pkce")
            raise SpotifyError("That connection attempt expired. Start again with “Connect Spotify”.")
        if state and not secrets.compare_digest(state, pk["state"]):
            raise SpotifyError("That address belongs to a different connection attempt. Start again with “Connect Spotify”.")
        self.db.kv_delete("spotify_pkce")                              # single use, whatever happens next
        t = self._token_request(grant_type="authorization_code", code=code, redirect_uri=self.s.spotify_redirect_uri, code_verifier=pk["verifier"])
        self.db.kv_set("spotify_token", {"access_token": t["access_token"], "refresh_token": t.get("refresh_token", ""),
                                         "expires_at": time.time() + int(t.get("expires_in", 3600)), "name": ""})
        try:
            me = self._api("GET", "/me")
        except SpotifyError:
            self._forget()
            raise
        name = (me.get("display_name") or me.get("id") or "Spotify user")[:60]
        tok = self.db.kv_get("spotify_token")
        self.db.kv_set("spotify_token", {**tok, "name": name, "user_id": me.get("id", ""), "product": me.get("product", "")})
        return name

    def _access_token(self, force: bool = False) -> str:
        tok = self.db.kv_get("spotify_token")
        if not tok:
            raise SpotifyError("Spotify is not connected.")
        if force or tok["expires_at"] - 60 < time.time():
            if not tok.get("refresh_token"):
                self._forget()
                raise SpotifyError(MSG_RECONNECT)
            try:
                t = self._token_request(grant_type="refresh_token", refresh_token=tok["refresh_token"])
            except SpotifyError as e:
                if "refused the login (4" in str(e):                    # revoked or expired for good
                    self._forget()
                    raise SpotifyError(MSG_RECONNECT) from e
                raise
            tok.update(access_token=t["access_token"], expires_at=time.time() + int(t.get("expires_in", 3600)))
            if t.get("refresh_token"):
                tok["refresh_token"] = t["refresh_token"]
            self.db.kv_set("spotify_token", tok)
        return tok["access_token"]

    # ---- API --------------------------------------------------------------------------------------------
    def _api(self, method: str, path: str, *, params=None, json=None) -> dict:
        refreshed = False
        while True:
            token = self._access_token(force=refreshed)
            try:
                r = self.http.request(method, API + path, params=params, json=json, headers={"Authorization": f"Bearer {token}"}, timeout=20)
            except httpx.HTTPError as e:
                raise SpotifyError(MSG_NETWORK) from e
            if r.status_code == 401 and not refreshed:
                refreshed = True
                continue
            if r.status_code == 401:
                self._forget()
                raise SpotifyError(MSG_RECONNECT)
            if r.status_code == 429:
                raise SpotifyError(MSG_RATE)
            if r.status_code == 403:
                raise SpotifyError(MSG_FORBIDDEN)
            if r.status_code in (404, 405):
                raise SpotifyError(f"NOT_FOUND:{r.status_code}")
            if r.status_code >= 400:
                raise SpotifyError(f"Spotify request failed ({r.status_code}).")
            try:
                return r.json() if r.content else {}
            except ValueError:
                return {}

    def search_track(self, artist: str, title: str) -> dict | None:
        """Best Spotify match for a song: {uri, seconds, artist, title} or None."""
        try:
            data = self._api("GET", "/search", params={"q": f'track:"{title}" artist:"{artist}"', "type": "track", "limit": 5})
        except SpotifyError as e:
            if str(e).startswith("NOT_FOUND"):
                return None
            raise
        for it in (data.get("tracks") or {}).get("items") or []:
            try:
                names = [a["name"] for a in it["artists"]]
                same_artist = any(_norm(artist) in _norm(n) or _norm(n) in _norm(artist) for n in names)
                same_title = _norm(title) in _norm(it["name"]) or _norm(it["name"]) in _norm(title)
                if same_artist and same_title and it.get("uri", "").startswith("spotify:track:"):
                    return {"uri": it["uri"], "seconds": round(int(it["duration_ms"]) / 1000), "artist": names[0], "title": it["name"]}
            except (KeyError, TypeError, ValueError):
                continue
        return None

    def create_playlist(self, name: str, description: str, uris: list[str]) -> dict:
        """Create a private playlist containing `uris`. Returns {id, url, added}."""
        if not uris:
            raise SpotifyError("There are no songs to add.")
        body = {"name": name[:100], "description": description[:300], "public": False}
        try:
            pl = self._api("POST", "/me/playlists", json=body)                   # current endpoint
        except SpotifyError as e:
            if not str(e).startswith("NOT_FOUND"):
                raise
            uid = self.db.kv_get("spotify_token", {}).get("user_id") or self._api("GET", "/me").get("id")
            pl = self._api("POST", f"/users/{uid}/playlists", json=body)         # older endpoint
        pid = pl.get("id")
        if not pid:
            raise SpotifyError("Spotify didn't return the new playlist.")
        for i in range(0, len(uris), 100):
            chunk = {"uris": uris[i:i + 100]}
            try:
                self._api("POST", f"/playlists/{pid}/items", json=chunk)
            except SpotifyError as e:
                if not str(e).startswith("NOT_FOUND"):
                    raise
                self._api("POST", f"/playlists/{pid}/tracks", json=chunk)
        return {"id": pid, "url": (pl.get("external_urls") or {}).get("spotify") or f"https://open.spotify.com/playlist/{pid}", "added": len(uris)}

    def disconnect(self) -> None:
        self._forget()
        self.db.kv_delete("spotify_pkce")
