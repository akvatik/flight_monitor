from __future__ import annotations

import json
import logging
from decimal import Decimal, ROUND_HALF_UP
from urllib.request import Request, urlopen

from .models import FlightOffer
from .presentation import RichMarkdown

LOG = logging.getLogger(__name__)


def _rub(value: Decimal) -> str:
    return f"{int(value.quantize(Decimal('1'), rounding=ROUND_HALF_UP)):,}".replace(",", " ")


def drop_message(offer: FlightOffer, previous: Decimal) -> str:
    return ("🔥 Новый минимум\n\n"
            f"{offer.flight_number}\n"
            f"{offer.origin_airport} → {offer.destination_airport}\n"
            f"{offer.departure_local:%d.%m.%Y %H:%M} → {offer.arrival_local:%H:%M}\n\n"
            f"Цена: {_rub(offer.price)} ₽\n"
            f"Предыдущий минимум: {_rub(previous)} ₽\n"
            f"Снижение: {_rub(previous - offer.price)} ₽\n\n"
            "Источник: OneTwoTrip\nПроверено повторным поиском.")


class TelegramNotifier:
    def __init__(self, token: str | None, chat_id: str | None):
        self.token = token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _request(self, method: str, payload: dict, *, timeout: int = 20):
        if not self.token:
            raise RuntimeError("Telegram bot token is not configured")
        try:
            url = f"https://api.telegram.org/bot{self.token}/{method}"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=timeout) as response:
                result = json.load(response)
            if not result.get("ok"):
                raise RuntimeError("Telegram returned an unsuccessful result")
            return result.get("result")
        except Exception as exc:
            # HTTP exceptions may contain the URL and bot token.
            LOG.error("Telegram %s failed (%s)", method, type(exc).__name__)
            raise RuntimeError("Telegram delivery failed") from None

    def send_to(self, chat_id: int | str, message: str, *, parse_mode: str | None = None,
                reply_markup: dict | None = None) -> bool:
        if not self.token:
            LOG.info("Telegram disabled.")
            return False
        try:
            if isinstance(message, RichMarkdown):
                payload = {"chat_id": chat_id,
                           "rich_message": {"markdown": str(message)}}
                method = "sendRichMessage"
            else:
                payload = {"chat_id": chat_id, "text": message}
                if parse_mode:
                    payload["parse_mode"] = parse_mode
                method = "sendMessage"
            if reply_markup:
                payload["reply_markup"] = reply_markup
            self._request(method, payload)
            LOG.info("Telegram sent")
            return True
        except RuntimeError:
            raise

    def send(self, message: str, *, parse_mode: str | None = None) -> bool:
        if not self.enabled:
            LOG.info("Telegram disabled.")
            return False
        return self.send_to(self.chat_id, message, parse_mode=parse_mode)

    def send_report(self, chat_id: int | str, report: list[RichMarkdown]) -> None:
        for message in report:
            self.send_to(chat_id, message)

    def broadcast_report(self, chat_ids: list[int], report: list[RichMarkdown],
                         *, exclude_chat_ids: set[int] | None = None) -> tuple[int, int]:
        excluded = exclude_chat_ids or set()
        recipients = [chat_id for chat_id in chat_ids if chat_id not in excluded]
        sent = failed = 0
        for chat_id in recipients:
            try:
                self.send_report(chat_id, report)
                sent += 1
            except RuntimeError:
                failed += 1
        LOG.info("Telegram broadcast recipients=%d sent=%d failed=%d", len(recipients), sent, failed)
        return sent, failed

    def get_updates(self, offset: int | None, *, timeout: int = 30) -> list[dict]:
        payload = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self._request("getUpdates", payload, timeout=timeout + 10) or []

    def answer_callback_query(self, callback_query_id: str) -> None:
        self._request("answerCallbackQuery", {"callback_query_id": callback_query_id})

    def get_bot_username(self) -> str:
        result = self._request("getMe", {})
        username = result.get("username") if isinstance(result, dict) else None
        if not isinstance(username, str) or not username:
            raise RuntimeError("Telegram bot username unavailable")
        return username


class BroadcastNotifier:
    """Route existing MonitorService notifications to registered recipients."""

    def __init__(self, api: TelegramNotifier, chat_ids: list[int],
                 *, exclude_chat_ids: set[int] | None = None):
        self.api = api
        self.chat_ids = chat_ids
        self.exclude_chat_ids = exclude_chat_ids or set()

    @property
    def enabled(self) -> bool:
        return bool(self.api.token and self.chat_ids)

    def send(self, message: str, *, parse_mode: str | None = None) -> bool:
        recipients = [chat_id for chat_id in self.chat_ids if chat_id not in self.exclude_chat_ids]
        sent = failed = 0
        for chat_id in recipients:
            try:
                self.api.send_to(chat_id, message, parse_mode=parse_mode)
                sent += 1
            except RuntimeError:
                failed += 1
        LOG.info("Telegram broadcast recipients=%d sent=%d failed=%d", len(recipients), sent, failed)
        return failed == 0
