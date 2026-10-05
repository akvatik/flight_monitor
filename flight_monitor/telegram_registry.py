"""Transactional Telegram membership, invites, cooldown and check lease."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import logging
import math
import secrets

LOG = logging.getLogger(__name__)
CHECK_LOCK_NAME = "flight-monitor-check"
REGISTRY_ADVISORY_LOCK = 572393813


@dataclass(frozen=True)
class ManualReservation:
    status: str
    owner_id: str | None = None
    wait_seconds: int = 0


class TelegramRegistry:
    def __init__(self, db, *, dialect: str):
        if dialect not in {"sqlite", "postgres"}:
            raise ValueError("Unsupported registry database")
        self.db = db
        self.dialect = dialect

    def _stamp(self, value: datetime):
        return value.isoformat() if self.dialect == "sqlite" else value

    @staticmethod
    def _datetime(value) -> datetime:
        return value if isinstance(value, datetime) else datetime.fromisoformat(value)

    def _execute(self, sql: str, params=()):
        if self.dialect == "postgres":
            sql = sql.replace("?", "%s")
        return self.db.execute(sql, params)

    @contextmanager
    def _transaction(self):
        if self.dialect == "sqlite":
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.rollback()
                raise
            else:
                self.db.commit()
        else:
            with self.db.transaction():
                self.db.execute("SELECT pg_advisory_xact_lock(%s)", (REGISTRY_ADVISORY_LOCK,))
                yield

    def user(self, chat_id: int):
        return self._execute("SELECT * FROM telegram_users WHERE chat_id=?", (chat_id,)).fetchone()

    def is_active(self, chat_id: int) -> bool:
        row = self.user(chat_id)
        return bool(row and row["active"])

    def is_owner(self, chat_id: int) -> bool:
        row = self.user(chat_id)
        return bool(row and row["active"] and row["is_owner"])

    def active_chat_ids(self) -> list[int]:
        rows = self._execute("SELECT chat_id FROM telegram_users WHERE active=? ORDER BY chat_id",
                             (1 if self.dialect == "sqlite" else True,)).fetchall()
        return [int(row["chat_id"]) for row in rows]

    def _active_count(self) -> int:
        return int(self._execute("SELECT COUNT(*) AS total FROM telegram_users WHERE active=?",
                                 (1 if self.dialect == "sqlite" else True,)).fetchone()["total"])

    def bootstrap_owner(self, owner_chat_id: str | None, max_users: int,
                        *, now: datetime | None = None) -> bool:
        if not owner_chat_id:
            return False
        try:
            chat_id = int(owner_chat_id)
        except ValueError:
            raise ValueError("TELEGRAM_OWNER_CHAT_ID must be numeric") from None
        timestamp = now or datetime.now(timezone.utc)
        with self._transaction():
            existing_owner = self._execute("SELECT chat_id FROM telegram_users WHERE is_owner=? LIMIT 1",
                                           (1 if self.dialect == "sqlite" else True,)).fetchone()
            if existing_owner is not None:
                return False
            current = self.user(chat_id)
            if (current is None or not current["active"]) and self._active_count() >= max_users:
                raise RuntimeError("Cannot bootstrap owner: Telegram user limit reached")
            if current is None:
                self._execute("""INSERT INTO telegram_users
                    (chat_id,username,first_name,registered_at,active,is_owner,last_manual_check_at)
                    VALUES(?,?,?,?,?,?,NULL)""",
                    (chat_id, None, None, self._stamp(timestamp), True, True))
            else:
                self._execute("UPDATE telegram_users SET active=?,is_owner=? WHERE chat_id=?",
                              (True, True, chat_id))
        LOG.info("Telegram owner bootstrapped")
        return True

    def create_invite(self, owner_chat_id: int, max_users: int, ttl_hours: int,
                      *, now: datetime | None = None) -> tuple[str, str | None]:
        timestamp = now or datetime.now(timezone.utc)
        with self._transaction():
            if not self.is_owner(owner_chat_id):
                return "forbidden", None
            if self._active_count() >= max_users:
                return "limit", None
            token = secrets.token_urlsafe(32)
            digest = sha256(token.encode("utf-8")).hexdigest()
            self._execute("""INSERT INTO telegram_invites
                (token_hash,created_by_chat_id,created_at,expires_at,used_at,used_by_chat_id)
                VALUES(?,?,?,?,NULL,NULL)""",
                (digest, owner_chat_id, self._stamp(timestamp),
                 self._stamp(timestamp + timedelta(hours=ttl_hours))))
        LOG.info("Telegram invite created")
        return "created", token

    def redeem_invite(self, token: str, chat_id: int, username: str | None,
                      first_name: str | None, max_users: int,
                      *, now: datetime | None = None) -> str:
        timestamp = now or datetime.now(timezone.utc)
        digest = sha256(token.encode("utf-8")).hexdigest()
        with self._transaction():
            existing = self.user(chat_id)
            if existing is not None:
                return "already_registered" if existing["active"] else "inactive"
            invite = self._execute("SELECT * FROM telegram_invites WHERE token_hash=?",
                                   (digest,)).fetchone()
            if invite is None or invite["used_at"] is not None:
                return "invalid"
            if self._datetime(invite["expires_at"]) <= timestamp:
                return "expired"
            if self._active_count() >= max_users:
                return "limit"
            self._execute("""INSERT INTO telegram_users
                (chat_id,username,first_name,registered_at,active,is_owner,last_manual_check_at)
                VALUES(?,?,?,?,?,?,NULL)""",
                (chat_id, username, first_name, self._stamp(timestamp), True, False))
            self._execute("""UPDATE telegram_invites SET used_at=?,used_by_chat_id=?
                WHERE id=? AND used_at IS NULL""",
                (self._stamp(timestamp), chat_id, invite["id"]))
        LOG.info("Telegram invite consumed; user registered")
        return "registered"

    def list_users(self, owner_chat_id: int):
        if not self.is_owner(owner_chat_id):
            return None
        return self._execute("""SELECT username,first_name,active,is_owner
            FROM telegram_users ORDER BY is_owner DESC,registered_at""").fetchall()

    def _acquire_lock_in_transaction(self, owner_id: str, ttl_seconds: int,
                                     now: datetime) -> bool:
        cursor = self._execute("""INSERT INTO check_locks
            (lock_name,owner_id,locked_until,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(lock_name) DO UPDATE SET
                owner_id=excluded.owner_id,
                locked_until=excluded.locked_until,
                updated_at=excluded.updated_at
            WHERE check_locks.locked_until<=excluded.updated_at""",
            (CHECK_LOCK_NAME, owner_id, self._stamp(now + timedelta(seconds=ttl_seconds)),
             self._stamp(now)))
        return cursor.rowcount == 1

    def acquire_check_lock(self, owner_id: str, ttl_seconds: int,
                           *, now: datetime | None = None) -> bool:
        timestamp = now or datetime.now(timezone.utc)
        with self._transaction():
            return self._acquire_lock_in_transaction(owner_id, ttl_seconds, timestamp)

    def reserve_manual_check(self, chat_id: int, cooldown_seconds: int,
                             ttl_seconds: int, *, now: datetime | None = None) -> ManualReservation:
        timestamp = now or datetime.now(timezone.utc)
        owner_id = secrets.token_urlsafe(24)
        with self._transaction():
            member = self.user(chat_id)
            if not member or not member["active"]:
                return ManualReservation("unauthorized")
            last = member["last_manual_check_at"]
            if last is not None:
                remaining = cooldown_seconds - (timestamp - self._datetime(last)).total_seconds()
                if remaining > 0:
                    return ManualReservation("cooldown", wait_seconds=math.ceil(remaining))
            if not self._acquire_lock_in_transaction(owner_id, ttl_seconds, timestamp):
                return ManualReservation("busy")
            self._execute("UPDATE telegram_users SET last_manual_check_at=? WHERE chat_id=?",
                          (self._stamp(timestamp), chat_id))
        LOG.info("Manual check requested")
        return ManualReservation("accepted", owner_id=owner_id)

    def refresh_check_lock(self, owner_id: str, ttl_seconds: int,
                           *, now: datetime | None = None) -> bool:
        timestamp = now or datetime.now(timezone.utc)
        with self._transaction():
            cursor = self._execute("""UPDATE check_locks SET locked_until=?,updated_at=?
                WHERE lock_name=? AND owner_id=? AND locked_until>?""",
                (self._stamp(timestamp + timedelta(seconds=ttl_seconds)), self._stamp(timestamp),
                 CHECK_LOCK_NAME, owner_id, self._stamp(timestamp)))
            return cursor.rowcount == 1

    def release_check_lock(self, owner_id: str) -> None:
        with self._transaction():
            self._execute("DELETE FROM check_locks WHERE lock_name=? AND owner_id=?",
                          (CHECK_LOCK_NAME, owner_id))
