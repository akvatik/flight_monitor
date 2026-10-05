from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Protocol

from .config import Config
from .models import BaggageCategory, FlightOffer, minimum
from .notifier import TelegramNotifier, drop_message
from .presentation import telegram_summary_messages
from .source import FlightSource

LOG = logging.getLogger(__name__)


class PriceStore(Protocol):
    def historical_minimum(self) -> Decimal | None: ...
    def previous_successful_check_minimum(self) -> Decimal | None: ...
    def previous_successful_baggage_prices(self) -> dict[tuple[str, BaggageCategory], Decimal]: ...
    def start_run(self) -> int: ...
    def save_offers(self, run_id: int, offers: list[FlightOffer], *, confirmed: bool = False) -> None: ...
    def finish_run(self, run_id: int, status: str, error_message: str | None = None) -> None: ...
    def confirm_run_offers(self, run_id: int) -> None: ...
    def confirm_offer(self, run_id: int, offer: FlightOffer) -> None: ...
    def record_notification(self, kind: str, offer: FlightOffer, previous: Decimal | None,
                            status: str, error: str | None = None) -> None: ...


@dataclass(frozen=True)
class CheckResult:
    offers: list[FlightOffer]
    current_minimum: FlightOffer | None
    historical_before: Decimal | None
    historical_after: Decimal | None
    status: str
    rechecked: FlightOffer | None = None


class MonitorService:
    def __init__(self, config: Config, source: FlightSource, repo: PriceStore,
                 notifier: TelegramNotifier, sleep: Callable[[float], None] = time.sleep):
        self.config = config
        self.source = source
        self.repo = repo
        self.notifier = notifier
        self.sleep = sleep

    def _search(self) -> list[FlightOffer]:
        c = self.config
        return self.source.search(c.origin, c.destination, c.departure_date,
                                  c.adults, c.cabin_class, c.direct_only)

    def _send_summary(self, offers: list[FlightOffer],
                      previous_prices: dict[tuple[str, BaggageCategory], Decimal],
                      *, dry_run: bool, checked_at_utc: datetime) -> None:
        if not self.config.telegram_notify_every_run:
            return
        if dry_run:
            LOG.info("Telegram run summary skipped (dry run)")
            return
        if not self.notifier.enabled:
            return
        for message in telegram_summary_messages(offers, previous_prices,
                                                 checked_at_utc=checked_at_utc):
            self.notifier.send(message, parse_mode="HTML")

    def check(self, *, dry_run: bool = False) -> CheckResult:
        if not self.notifier.enabled:
            LOG.info("Telegram disabled.")
        previous_prices = (self.repo.previous_successful_baggage_prices()
                           if self.config.telegram_notify_every_run else {})
        before = self.repo.historical_minimum()
        first_run = self.repo.start_run()
        LOG.info("Starting search run %d", first_run)
        try:
            offers = self._search()
            checked_at_utc = offers[0].checked_at_utc if offers else datetime.now(timezone.utc)
            self.repo.save_offers(first_run, offers)
        except Exception as exc:
            self.repo.finish_run(first_run, "failed", str(exc)[:300])
            LOG.error("Search failed: %s", exc)
            raise
        current = minimum(offers)
        LOG.info("Current minimum: %s; historical confirmed minimum: %s",
                 current.price if current else None, before)
        if current is None:
            self.repo.finish_run(first_run, "empty")
            self._send_summary(offers, previous_prices, dry_run=dry_run,
                               checked_at_utc=checked_at_utc)
            return CheckResult(offers, None, before, before, "empty")
        if before is None:
            # A successful first observation sets the baseline and does not notify.
            self.repo.confirm_run_offers(first_run)
            self.repo.finish_run(first_run, "baseline")
            LOG.info("Baseline established at %s; Telegram skipped", current.price)
            self._send_summary(offers, previous_prices, dry_run=dry_run,
                               checked_at_utc=checked_at_utc)
            return CheckResult(offers, current, None, current.price, "baseline")
        if current.price >= before:
            self.repo.finish_run(first_run, "observed")
            LOG.info("No new minimum; Telegram skipped")
            self._send_summary(offers, previous_prices, dry_run=dry_run,
                               checked_at_utc=checked_at_utc)
            return CheckResult(offers, current, before, before, "observed")

        LOG.info("Possible drop to %s; starting fresh recheck after %s seconds",
                 current.price, self.config.recheck_delay_seconds)
        self.repo.finish_run(first_run, "candidate")
        self.sleep(self.config.recheck_delay_seconds)
        second_run = self.repo.start_run()
        try:
            rechecked_offers = self._search()
            self.repo.save_offers(second_run, rechecked_offers)
        except Exception as exc:
            self.repo.finish_run(second_run, "failed", str(exc)[:300])
            LOG.error("Recheck failed: %s", exc)
            raise
        same_flight = [offer for offer in rechecked_offers if offer.flight_key == current.flight_key]
        verified = minimum(same_flight)
        if verified is None or verified.price > current.price:
            self.repo.finish_run(second_run, "unconfirmed")
            LOG.info("Drop unconfirmed; recheck price: %s", verified.price if verified else None)
            self._send_summary(offers, previous_prices, dry_run=dry_run,
                               checked_at_utc=checked_at_utc)
            return CheckResult(offers, current, before, before, "unconfirmed", verified)

        self.repo.confirm_offer(second_run, verified)
        self.repo.finish_run(second_run, "confirmed")
        LOG.info("Drop confirmed on fresh search: %s", verified.price)
        if dry_run:
            LOG.info("Telegram skipped (dry run)")
            self.repo.record_notification("new_minimum", verified, before, "dry_run")
        elif not self.notifier.enabled:
            self.repo.record_notification("new_minimum", verified, before, "disabled")
        else:
            try:
                self.notifier.send(drop_message(verified, before))
                self.repo.record_notification("new_minimum", verified, before, "sent")
            except RuntimeError:
                self.repo.record_notification("new_minimum", verified, before, "failed", "delivery failed")
                raise
        self._send_summary(offers, previous_prices, dry_run=dry_run,
                           checked_at_utc=checked_at_utc)
        return CheckResult(offers, current, before, verified.price, "confirmed", verified)
