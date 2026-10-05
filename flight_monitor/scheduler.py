"""Periodic infrastructure runner for the existing scheduled check."""

from __future__ import annotations

import logging
import math
import os
import signal
from threading import Event

from .check_runner import run_scheduled_check
from .config import Config


LOG = logging.getLogger(__name__)


def run_scheduler(stop: Event | None = None, *, check=run_scheduled_check) -> None:
    config = Config.from_env()
    try:
        minutes = float(os.getenv("CHECK_INTERVAL_MINUTES", "60"))
    except ValueError:
        raise ValueError("CHECK_INTERVAL_MINUTES must be a positive number") from None
    if not math.isfinite(minutes) or minutes <= 0:
        raise ValueError("CHECK_INTERVAL_MINUTES must be a positive number")

    stop = stop or Event()
    LOG.info("Scheduler started; interval_minutes=%s", minutes)
    # First check waits for one interval. This prevents an automatic search on startup.
    while not stop.wait(minutes * 60):
        try:
            result = check(config)
            LOG.info("Scheduled check finished; status=%s",
                     "skipped_locked" if result is None else result.status)
        except Exception as exc:
            LOG.error("Scheduled check failed (%s)", type(exc).__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_args: stop.set())
    signal.signal(signal.SIGINT, lambda *_args: stop.set())
    run_scheduler(stop)


if __name__ == "__main__":
    main()
