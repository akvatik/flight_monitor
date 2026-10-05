from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv


ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


@dataclass(frozen=True)
class Config:
    origin: str
    destination: str
    departure_date: date
    adults: int
    cabin_class: str
    direct_only: bool
    recheck_delay_seconds: float
    database_path: Path
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    browser_channel: str | None
    browser_timeout_seconds: float
    telegram_notify_every_run: bool = False
    database_url: str | None = None
    telegram_owner_chat_id: str | None = None
    max_telegram_users: int = 3
    telegram_invite_ttl_hours: int = 24
    manual_check_cooldown_seconds: int = 180
    check_lock_ttl_seconds: int = 600

    @property
    def owner_chat_id(self) -> str | None:
        return self.telegram_owner_chat_id or self.telegram_chat_id

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv(dotenv_path=ENV_FILE, override=False)
        db_url = os.getenv("DATABASE_URL")
        if db_url:
            if db_url.startswith("sqlite:///"):
                db_path = Path(db_url.removeprefix("sqlite:///"))
            elif db_url.startswith(("postgresql://", "postgres://")):
                db_path = Path(os.getenv("DATABASE_PATH", "data/flight_monitor.sqlite3"))
            else:
                raise ValueError("DATABASE_URL must use sqlite:/// or postgresql://")
        else:
            db_path = Path(os.getenv("DATABASE_PATH", "data/flight_monitor.sqlite3"))
        max_users = int(os.getenv("MAX_TELEGRAM_USERS", "3"))
        invite_ttl = int(os.getenv("TELEGRAM_INVITE_TTL_HOURS", "24"))
        cooldown = int(os.getenv("MANUAL_CHECK_COOLDOWN_SECONDS", "180"))
        lock_ttl = int(os.getenv("CHECK_LOCK_TTL_SECONDS", "600"))
        if max_users < 1 or invite_ttl < 1 or cooldown < 0 or lock_ttl < 1:
            raise ValueError("Invalid Telegram user, invite, cooldown or lock setting")
        return cls(
            origin=os.getenv("ORIGIN", "LED").upper(),
            destination=os.getenv("DESTINATION", "MOW").upper(),
            departure_date=date.fromisoformat(os.getenv("DEPARTURE_DATE", "2026-11-04")),
            adults=int(os.getenv("ADULTS", "1")),
            cabin_class=os.getenv("CABIN_CLASS", "E").upper(),
            direct_only=os.getenv("DIRECT_ONLY", "true").lower() in ("1", "true", "yes"),
            recheck_delay_seconds=float(os.getenv("RECHECK_DELAY_SECONDS", "30")),
            database_path=db_path,
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
            browser_channel=os.getenv("BROWSER_CHANNEL") or None,
            browser_timeout_seconds=float(os.getenv("BROWSER_TIMEOUT_SECONDS", "60")),
            telegram_notify_every_run=os.getenv("TELEGRAM_NOTIFY_EVERY_RUN", "false").lower()
            in ("1", "true", "yes"),
            database_url=db_url,
            telegram_owner_chat_id=os.getenv("TELEGRAM_OWNER_CHAT_ID") or None,
            max_telegram_users=max_users,
            telegram_invite_ttl_hours=invite_ttl,
            manual_check_cooldown_seconds=cooldown,
            check_lock_ttl_seconds=lock_ttl,
        )
