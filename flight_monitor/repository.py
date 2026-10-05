from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .migrations import apply_sqlite_migrations
from .models import BaggageCategory, FlightOffer
from .telegram_registry import TelegramRegistry


class PriceRepository:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS search_runs (
                id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
                completed_at TEXT, status TEXT NOT NULL,
                source TEXT NOT NULL, error_message TEXT
            );
            CREATE TABLE IF NOT EXISTS offers (
                id INTEGER PRIMARY KEY, search_run_id INTEGER NOT NULL REFERENCES search_runs(id),
                checked_at_utc TEXT NOT NULL, source TEXT NOT NULL,
                marketing_carrier TEXT NOT NULL, operating_carrier TEXT,
                flight_number TEXT NOT NULL, flight_key TEXT NOT NULL,
                origin_airport TEXT NOT NULL, destination_airport TEXT NOT NULL,
                departure_local TEXT NOT NULL, arrival_local TEXT NOT NULL,
                timezone TEXT NOT NULL, direct INTEGER NOT NULL,
                price TEXT NOT NULL, currency TEXT NOT NULL,
                seats_available INTEGER, search_id TEXT, offer_id TEXT,
                confirmed INTEGER NOT NULL DEFAULT 0,
                baggage_category TEXT, baggage_code TEXT
            );
            CREATE INDEX IF NOT EXISTS offers_flight_idx ON offers(flight_key);
            CREATE INDEX IF NOT EXISTS offers_confirmed_idx ON offers(confirmed);
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL,
                type TEXT NOT NULL, flight_key TEXT NOT NULL,
                price TEXT NOT NULL, previous_price TEXT,
                status TEXT NOT NULL, error_message TEXT
            );
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(offers)")}
        for name in ("baggage_category", "baggage_code"):
            if name not in columns:
                self.db.execute(f"ALTER TABLE offers ADD COLUMN {name} TEXT")
        self.db.commit()
        apply_sqlite_migrations(self.db)
        self.registry = TelegramRegistry(self.db, dialect="sqlite")

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "PriceRepository":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def start_run(self) -> int:
        cur = self.db.execute(
            "INSERT INTO search_runs(started_at,status,source) VALUES(?,?,?)",
            (datetime.now(timezone.utc).isoformat(), "running", "onetwotrip"))
        self.db.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, error_message: str | None = None) -> None:
        self.db.execute("UPDATE search_runs SET completed_at=?,status=?,error_message=? WHERE id=?",
                        (datetime.now(timezone.utc).isoformat(), status, error_message, run_id))
        self.db.commit()

    def save_offers(self, run_id: int, offers: list[FlightOffer], *, confirmed: bool = False) -> None:
        self.db.executemany("""INSERT INTO offers (
            search_run_id, checked_at_utc, source, marketing_carrier, operating_carrier,
            flight_number, flight_key, origin_airport, destination_airport,
            departure_local, arrival_local, timezone, direct, price, currency,
            seats_available, search_id, offer_id, confirmed, baggage_category, baggage_code
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [(
            run_id, offer.checked_at_utc.isoformat(), offer.source, offer.marketing_carrier,
            offer.operating_carrier, offer.flight_number, offer.flight_key, offer.origin_airport,
            offer.destination_airport, offer.departure_local.isoformat(),
            offer.arrival_local.isoformat(), offer.timezone, int(offer.direct),
            str(offer.price), offer.currency, offer.seats_available,
            offer.search_id, offer.offer_id, int(confirmed),
            offer.baggage_category.value, offer.baggage) for offer in offers])
        self.db.commit()

    def confirm_offer(self, run_id: int, offer: FlightOffer) -> None:
        cur = self.db.execute("""UPDATE offers SET confirmed=1 WHERE search_run_id=?
            AND flight_key=? AND price=? AND offer_id=?""",
            (run_id, offer.flight_key, str(offer.price), offer.offer_id))
        if cur.rowcount != 1:
            raise RuntimeError("Could not mark rechecked offer as confirmed")
        self.db.commit()

    def confirm_run_offers(self, run_id: int) -> None:
        self.db.execute("UPDATE offers SET confirmed=1 WHERE search_run_id=?", (run_id,))
        self.db.commit()

    def historical_minimum(self) -> Decimal | None:
        rows = self.db.execute("SELECT price FROM offers WHERE confirmed=1").fetchall()
        return min((Decimal(row[0]) for row in rows), default=None)

    def previous_successful_check_minimum(self) -> Decimal | None:
        # A recheck belongs to the preceding candidate. Compare the same first
        # search that the previous completed check showed in its summary.
        run = self.db.execute("""SELECT r.id FROM search_runs AS r
            LEFT JOIN search_runs AS next_run ON next_run.id =
                (SELECT MIN(id) FROM search_runs WHERE id > r.id)
            WHERE r.status IN ('baseline', 'observed', 'empty')
               OR (r.status = 'candidate' AND next_run.status IN ('confirmed', 'unconfirmed'))
            ORDER BY r.id DESC LIMIT 1""").fetchone()
        if run is None:
            return None
        rows = self.db.execute("SELECT price FROM offers WHERE search_run_id=?", (run["id"],)).fetchall()
        return min((Decimal(row["price"]) for row in rows), default=None)

    def previous_successful_baggage_prices(self) -> dict[tuple[str, BaggageCategory], Decimal]:
        # Every fresh search with offers is comparable, including a successful
        # recheck. Failed and empty searches cannot provide a price baseline.
        run = self.db.execute("""SELECT r.id FROM search_runs AS r
            WHERE r.status IN ('baseline', 'observed', 'candidate', 'confirmed', 'unconfirmed')
              AND EXISTS (SELECT 1 FROM offers WHERE search_run_id = r.id)
            ORDER BY r.id DESC LIMIT 1""").fetchone()
        if run is None:
            return {}
        rows = self.db.execute("""SELECT flight_key, baggage_category, price
            FROM offers WHERE search_run_id=?""", (run["id"],)).fetchall()
        result: dict[tuple[str, BaggageCategory], Decimal] = {}
        for row in rows:
            if row["baggage_category"] not in (
                    BaggageCategory.WITHOUT_BAGGAGE.value,
                    BaggageCategory.WITH_BAGGAGE.value):
                continue
            key = (row["flight_key"], BaggageCategory(row["baggage_category"]))
            price = Decimal(row["price"])
            if key not in result or price < result[key]:
                result[key] = price
        return result

    def record_notification(self, kind: str, offer: FlightOffer, previous: Decimal | None,
                            status: str, error: str | None = None) -> None:
        self.db.execute("""INSERT INTO notifications(timestamp,type,flight_key,price,
            previous_price,status,error_message) VALUES(?,?,?,?,?,?,?)""",
            (datetime.now(timezone.utc).isoformat(), kind, offer.flight_key, str(offer.price),
             str(previous) if previous is not None else None, status, error))
        self.db.commit()

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.db.execute("""SELECT r.id,r.started_at,r.status,r.error_message,
            COUNT(o.id) AS offers_count FROM search_runs r
            LEFT JOIN offers o ON o.search_run_id=r.id
            GROUP BY r.id ORDER BY r.id DESC LIMIT ?""", (limit,)).fetchall()

    def recent_offers(self, limit: int = 15) -> list[sqlite3.Row]:
        return self.db.execute("""SELECT checked_at_utc, flight_number, operating_carrier,
            origin_airport, destination_airport, departure_local, price, currency, confirmed
            FROM offers ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
