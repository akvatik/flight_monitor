"""Finite scheduled and manual entry points for the existing MonitorService."""

from __future__ import annotations

from dataclasses import replace
import logging
from threading import Event, Thread
from uuid import uuid4

from .config import Config
from .notifier import BroadcastNotifier, TelegramNotifier
from .presentation import telegram_summary_messages
from .service import CheckResult, MonitorService
from .source import OneTwoTripSource
from .storage import open_repository

LOG = logging.getLogger(__name__)


def _source(config: Config):
    return OneTwoTripSource(browser_channel=config.browser_channel,
                            timeout_seconds=config.browser_timeout_seconds)


class LeaseHeartbeat:
    def __init__(self, config: Config, owner_id: str):
        self.config = config
        self.owner_id = owner_id
        self.stop = Event()
        self.thread: Thread | None = None

    def __enter__(self):
        def renew() -> None:
            interval = max(1, self.config.check_lock_ttl_seconds / 3)
            while not self.stop.wait(interval):
                try:
                    with open_repository(self.config) as repo:
                        if not repo.registry.refresh_check_lock(
                                self.owner_id, self.config.check_lock_ttl_seconds):
                            LOG.error("Check lock lease was lost")
                            return
                except Exception:
                    LOG.error("Could not renew check lock lease")
        self.thread = Thread(target=renew, name="check-lock-heartbeat", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2)


def run_scheduled_check(config: Config, *, dry_run: bool = False,
                        source_factory=_source) -> CheckResult | None:
    with open_repository(config) as repo:
        repo.registry.bootstrap_owner(config.owner_chat_id, config.max_telegram_users)
        owner_id = uuid4().hex
        if not repo.registry.acquire_check_lock(owner_id, config.check_lock_ttl_seconds):
            LOG.info("Scheduled check skipped by lock")
            return None
        try:
            with LeaseHeartbeat(config, owner_id):
                api = TelegramNotifier(config.telegram_bot_token, config.owner_chat_id)
                broadcaster = BroadcastNotifier(api, repo.registry.active_chat_ids())
                return MonitorService(config, source_factory(config), repo, broadcaster).check(
                    dry_run=dry_run)
        finally:
            repo.registry.release_check_lock(owner_id)


def run_manual_check(config: Config, requester_chat_id: int, lock_owner_id: str,
                     api: TelegramNotifier, *, source_factory=_source) -> CheckResult:
    with open_repository(config) as repo:
        try:
            with LeaseHeartbeat(config, lock_owner_id):
                previous_prices = repo.previous_successful_baggage_prices()
                broadcaster = BroadcastNotifier(
                    api, repo.registry.active_chat_ids(),
                    exclude_chat_ids={requester_chat_id})
                # Manual requests always return one fresh report to the requester.
                # Production alerts still go to the other active users.
                manual_config = replace(config, telegram_notify_every_run=False)
                result = MonitorService(
                    manual_config, source_factory(config), repo, broadcaster).check()
                api.send_report(requester_chat_id, telegram_summary_messages(
                    result.offers, previous_prices,
                    checked_at_utc=result.offers[0].checked_at_utc if result.offers else None))
                return result
        finally:
            repo.registry.release_check_lock(lock_owner_id)
