"""Telegram update handling and local long-polling transport."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import math
import time
from typing import Callable

from .check_runner import run_manual_check
from .config import Config
from .notifier import TelegramNotifier
from .storage import open_repository

LOG = logging.getLogger(__name__)
CHECK_CALLBACK = "check_prices"
MENU = {"inline_keyboard": [[{"text": "🔄 Проверить цены сейчас",
                              "callback_data": CHECK_CALLBACK}]]}
UNAUTHORIZED = "⛔ У вас нет доступа к этому боту."
INVITE_ONLY = "Доступ к Flight Monitor предоставляется по приглашению владельца."
BUSY = "⏳ Проверка цен уже выполняется. Попробуйте через несколько минут."
SEARCH_ERROR = "⚠️ Не удалось получить свежие цены. Предыдущие данные не использованы."


class TelegramBotHandler:
    """Domain handler; the polling transport supplies decoded updates."""

    def __init__(self, config: Config, registry, api: TelegramNotifier,
                 submit_manual: Callable[[int, str], None]):
        self.config = config
        self.registry = registry
        self.api = api
        self.submit_manual = submit_manual
        self._bot_username: str | None = None

    def _send_menu(self, chat_id: int, *, welcome: bool = False) -> None:
        message = ("Добро пожаловать в Flight Monitor." if welcome
                   else "Flight Monitor готов к проверке цен.")
        self.api.send_to(chat_id, message, reply_markup=MENU)

    def _manual_request(self, chat_id: int) -> None:
        if not self.registry.is_active(chat_id):
            self.api.send_to(chat_id, UNAUTHORIZED)
            return
        reservation = self.registry.reserve_manual_check(
            chat_id, self.config.manual_check_cooldown_seconds,
            self.config.check_lock_ttl_seconds)
        if reservation.status == "busy":
            LOG.info("Manual check rejected by lock")
            self.api.send_to(chat_id, BUSY)
            return
        if reservation.status == "cooldown":
            LOG.info("Manual check rejected by cooldown")
            minutes = max(1, math.ceil(reservation.wait_seconds / 60))
            self.api.send_to(chat_id, f"⏳ Повторите проверку примерно через {minutes} мин.")
            return
        if reservation.status != "accepted" or reservation.owner_id is None:
            self.api.send_to(chat_id, UNAUTHORIZED)
            return
        try:
            self.api.send_to(chat_id, "🔎 Проверяю актуальные цены…")
        finally:
            self.submit_manual(chat_id, reservation.owner_id)

    def _start(self, chat_id: int, message: dict, token: str | None) -> None:
        if self.registry.is_active(chat_id):
            self._send_menu(chat_id)
            return
        if not token:
            self.api.send_to(chat_id, INVITE_ONLY)
            return
        sender = message.get("from") or {}
        status = self.registry.redeem_invite(
            token, chat_id, sender.get("username"), sender.get("first_name"),
            self.config.max_telegram_users)
        if status == "registered":
            self._send_menu(chat_id, welcome=True)
        elif status == "limit":
            self.api.send_to(chat_id, f"Достигнут лимит пользователей: {self.config.max_telegram_users}.")
        else:
            self.api.send_to(chat_id, "Приглашение недействительно или срок его действия истёк.")

    def _invite(self, chat_id: int) -> None:
        if not self.registry.is_owner(chat_id):
            self.api.send_to(chat_id, UNAUTHORIZED)
            return
        if self._bot_username is None:
            self._bot_username = self.api.get_bot_username()
        status, token = self.registry.create_invite(
            chat_id, self.config.max_telegram_users, self.config.telegram_invite_ttl_hours)
        if status == "forbidden":
            self.api.send_to(chat_id, UNAUTHORIZED)
            return
        if status == "limit":
            self.api.send_to(chat_id, f"Достигнут лимит пользователей: {self.config.max_telegram_users}.")
            return
        self.api.send_to(chat_id, f"Приглашение ({self.config.telegram_invite_ttl_hours} ч): "
                     f"https://t.me/{self._bot_username}?start={token}")

    def _users(self, chat_id: int) -> None:
        users = self.registry.list_users(chat_id)
        if users is None:
            self.api.send_to(chat_id, UNAUTHORIZED)
            return
        lines = ["Пользователи Flight Monitor:"]
        for row in users:
            name = row["first_name"] or row["username"] or "Без имени"
            name = str(name).replace("\r", " ").replace("\n", " ")[:80]
            role = "owner" if row["is_owner"] else "user"
            state = "active" if row["active"] else "inactive"
            lines.append(f"• {name} · {role} · {state}")
        self.api.send_to(chat_id, "\n".join(lines))

    def handle_update(self, update: dict) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            callback_id = callback.get("id")
            if isinstance(callback_id, str):
                try:
                    self.api.answer_callback_query(callback_id)
                except RuntimeError:
                    LOG.warning("Telegram callback acknowledgement failed")
            chat = (callback.get("message") or {}).get("chat") or {}
            if chat.get("type") != "private" or not isinstance(chat.get("id"), int):
                return
            if callback.get("data") == CHECK_CALLBACK:
                self._manual_request(chat["id"])
            return

        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or not isinstance(chat.get("id"), int):
            return
        chat_id = chat["id"]
        text = message.get("text")
        if not isinstance(text, str):
            return
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return
        command = parts[0].split("@", 1)[0]
        if command == "/start":
            self._start(chat_id, message, parts[1].strip() if len(parts) > 1 else None)
        elif command == "/check":
            self._manual_request(chat_id)
        elif command == "/invite":
            self._invite(chat_id)
        elif command == "/users":
            self._users(chat_id)


def run_bot(config: Config, *, api: TelegramNotifier | None = None) -> None:
    if not config.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required for bot mode")
    if not config.owner_chat_id:
        raise RuntimeError("TELEGRAM_OWNER_CHAT_ID or TELEGRAM_CHAT_ID is required for bot mode")
    api = api or TelegramNotifier(config.telegram_bot_token, config.owner_chat_id)
    with open_repository(config) as repo, ThreadPoolExecutor(max_workers=1) as executor:
        repo.registry.bootstrap_owner(config.owner_chat_id, config.max_telegram_users)

        handler = TelegramBotHandler(config, repo.registry, api,
                                     lambda chat_id, owner_id: executor.submit(
                                         execute_manual_check, config, chat_id, owner_id, api))
        LOG.info("Telegram bot polling started")
        offset: int | None = None
        while True:
            try:
                updates = api.get_updates(offset, timeout=30)
                for update in updates:
                    if not isinstance(update, dict):
                        continue
                    if isinstance(update.get("update_id"), int):
                        offset = update["update_id"] + 1
                    try:
                        handler.handle_update(update)
                    except Exception:
                        LOG.error("Telegram update handling failed")
            except RuntimeError:
                LOG.error("Telegram polling failed; retrying")
                time.sleep(2)


def execute_manual_check(config: Config, chat_id: int, owner_id: str,
                         api: TelegramNotifier) -> None:
    """Worker boundary: one failed search must not stop update polling."""
    try:
        run_manual_check(config, chat_id, owner_id, api)
    except Exception:
        LOG.error("Manual check failed")
        try:
            with open_repository(config) as cleanup_repo:
                cleanup_repo.registry.release_check_lock(owner_id)
        except Exception:
            LOG.error("Manual check lock cleanup failed; lease will expire")
        try:
            api.send_to(chat_id, SEARCH_ERROR)
        except RuntimeError:
            LOG.error("Could not send manual check error")
