from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .models import FlightOffer
from .parser import SchemaError, parse_offers

LOG = logging.getLogger(__name__)


class SourceError(RuntimeError):
    pass


class FlightSource(Protocol):
    def search(self, origin: str, destination: str, departure_date: date,
               adults: int, cabin_class: str, direct_only: bool) -> list[FlightOffer]: ...


class OneTwoTripSource:
    def __init__(self, *, browser_channel: str | None = None, timeout_seconds: float = 60):
        self.browser_channel = browser_channel
        self.timeout_seconds = timeout_seconds

    def search(self, origin: str, destination: str, departure_date: date,
               adults: int, cabin_class: str, direct_only: bool) -> list[FlightOffer]:
        if adults != 1 or cabin_class != "E" or not direct_only:
            raise ValueError("OneTwoTrip browser flow was verified only for one adult, economy, direct")
        if departure_date.year != 2026:
            raise ValueError("Date URL has no year; this monitor is configured for 2026")
        route = f"{departure_date:%d%m}{origin}{destination}"
        page_url = f"https://www.onetwotrip.com/ru/f/search/{route}?sc=E&p=1_0_0"
        local_browsers = Path(__file__).resolve().parent.parent / ".pw-browsers"
        if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and local_browsers.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local_browsers)
        LOG.info("Starting OneTwoTrip Playwright search: %s %s %s", origin, destination, departure_date)
        try:
            with sync_playwright() as playwright:
                launch_kwargs = {"headless": True}
                if self.browser_channel:
                    launch_kwargs["channel"] = self.browser_channel
                browser = playwright.chromium.launch(**launch_kwargs)
                try:
                    context = browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")
                    page = context.new_page()
                    blocked: list[int] = []

                    def observe(response):
                        if response.status in (403, 429) and "onetwotrip.com" in response.url:
                            blocked.append(response.status)

                    page.on("response", observe)

                    def matches(response):
                        url = urlparse(response.url)
                        return (url.netloc == "www.onetwotrip.com"
                                and url.path == "/_avia-search-proxy/search/v3"
                                and parse_qs(url.query).get("route") == [route]
                                and response.request.method == "GET")

                    try:
                        with page.expect_response(matches, timeout=self.timeout_seconds * 1000) as pending:
                            page.goto(page_url, wait_until="domcontentloaded",
                                      timeout=self.timeout_seconds * 1000)
                        response = pending.value
                        if response.status != 200:
                            raise SourceError(f"OneTwoTrip search HTTP {response.status}")
                        if "json" not in response.headers.get("content-type", ""):
                            raise SourceError("OneTwoTrip search did not return JSON")
                        checked = datetime.now(timezone.utc)
                        try:
                            raw = response.json()
                        except Exception as exc:
                            raise SourceError("Invalid OneTwoTrip JSON") from exc
                        offers = parse_offers(raw, origin=origin, destination=destination,
                                              departure_date=departure_date, adults=adults,
                                              cabin_class=cabin_class, direct_only=direct_only,
                                              checked_at_utc=checked)
                        LOG.info("Search completed: %d direct offers", len(offers))
                        return offers
                    except PlaywrightTimeout as exc:
                        body = page.locator("body").inner_text(timeout=3000).lower() if page.locator("body").count() else ""
                        if blocked:
                            raise SourceError(f"OneTwoTrip refused request: HTTP {blocked[-1]}") from exc
                        if "captcha" in body or "подтвердите" in body or "робот" in body:
                            raise SourceError("OneTwoTrip requested browser verification/CAPTCHA") from exc
                        raise SourceError("Timed out waiting for OneTwoTrip search JSON") from exc
                    finally:
                        context.close()
                finally:
                    browser.close()
        except (SchemaError, SourceError):
            raise
        except PlaywrightError as exc:
            raise SourceError(f"Playwright browser failure: {exc}") from exc
