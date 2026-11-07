from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    db_path: str = "data/coach.db"
    model: str = "claude-sonnet-5-5"
    effort: str = "medium"
    plan_engine: str = "model"                 # "model": the language model writes the day-by-day plan; "builder": code writes it (coach/builder.py)
    chat_model: str = "claude-haiku-5-5"      # the cheapest current model: chat needs speed and low cost, not deep planning
    chat_effort: str = "low"
    spotify_client_id: str = ""                # one Spotify app for the whole server (PKCE: no client secret needed)
    spotify_redirect_uri: str = "http://127.0.0.1:8888/callback"
    spotify_user_limit: int = 5                # Spotify developer-mode apps allow 5 authorized users
    playlist_daily_limit: int = 10             # playlists built per user per rolling 24 hours
    chat_daily_limit: int = 40                 # messages per user per rolling 24 hours (bounds the cost)
    strava_client_id: str = ""
    strava_client_secret: str = ""
    strava_athlete_limit: int = 10   # your Strava app's athlete capacity; connecting beyond it is refused politely
    public_url: str = "http://localhost:8000"
    allow_signups: bool = True
    invite_code: str = ""
    timezone: str = "UTC"
    sync_interval_min: int = 30
    auto_adjust: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            db_path=e("COACH_DB", "data/coach.db"),
            model=e("COACH_MODEL", "claude-sonnet-5-5"),
            effort=e("COACH_EFFORT", "medium"),
            plan_engine=(e("PLAN_ENGINE", "model") or "model").strip().lower(),
            chat_model=e("COACH_CHAT_MODEL", "claude-haiku-5-5"),
            chat_effort=e("COACH_CHAT_EFFORT", "low"),
            chat_daily_limit=int(e("CHAT_MESSAGES_PER_DAY", "40") or 40),
            spotify_client_id=e("SPOTIFY_CLIENT_ID", "").strip(),
            spotify_redirect_uri=e("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback").strip() or "http://127.0.0.1:8888/callback",
            spotify_user_limit=int(e("SPOTIFY_USER_LIMIT", "5") or 5),
            playlist_daily_limit=int(e("PLAYLISTS_PER_DAY", "10") or 10),
            strava_client_id=e("STRAVA_CLIENT_ID", ""),
            strava_client_secret=e("STRAVA_CLIENT_SECRET", ""),
            strava_athlete_limit=int(e("STRAVA_ATHLETE_LIMIT", "10") or 10),
            public_url=e("PUBLIC_URL", "http://localhost:8000").rstrip("/"),
            allow_signups=e("ALLOW_SIGNUPS", "true").lower() in ("1", "true", "yes"),
            invite_code=e("INVITE_CODE", ""),
            timezone=e("TZ", "UTC"),
            sync_interval_min=int(e("SYNC_INTERVAL_MIN", "30") or 0),
            auto_adjust=e("AUTO_ADJUST", "true").lower() in ("1", "true", "yes"),
        )

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def strava_configured(self) -> bool:
        return bool(self.strava_client_id and self.strava_client_secret)

    def today(self) -> date:
        return datetime.now(ZoneInfo(self.timezone)).date()
