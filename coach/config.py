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
    strava_client_id: str = ""
    strava_client_secret: str = ""
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
            strava_client_id=e("STRAVA_CLIENT_ID", ""),
            strava_client_secret=e("STRAVA_CLIENT_SECRET", ""),
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
