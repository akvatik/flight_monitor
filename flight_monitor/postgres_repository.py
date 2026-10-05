"""Synchronous PostgreSQL implementation of the existing PriceStore contract."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .migrations import apply_postgres_migrations
from .models import BaggageCategory, FlightOffer
from .telegram_registry import TelegramRegistry


class PostgresPriceRepository:
    def __init__(self, url: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise RuntimeError("Install psycopg[binary] to use PostgreSQL") from None
        try:
            self.db = psycopg.connect(url, row_factory=dict_row, autocommit=True,
                                      connect_timeout=10)
        except Exception:
            # Driver exceptions can include a URL with credentials.
            raise RuntimeError("PostgreSQL connection failed") from None
        try:
            apply_postgres_migrations(self.db)
        except Exception:
            self.db.close()
            raise RuntimeError("PostgreSQL schema migration failed") from None
        self.registry = TelegramRegistry(self.db, dialect="postgres")

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "PostgresPriceRepository":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def start_run(self) -> int:
        row = self.db.execute("""INSERT INTO search_runs(started_at,status,source)
            VALUES(%s,%s,%s) RETURNING id""",
            (datetime.now(timezone.utc).isoformat(), "running", "onetwotrip")).fetchone()
        return int(row["id"])

    def finish_run(self, run_id: int, status: str, error_message: str | None = None) -> None:
        self.db.execute("UPDATE search_runs SET completed_at=%s,status=%s,error_message=%s WHERE id=%s",
                        (datetime.now(timezone.utc).isoformat(), status, error_message, run_id))

    def save_offers(self, run_id: int, offers: list[FlightOffer], *, confirmed: bool = False) -> None:
        values = [(
            run_id, offer.checked_at_utc.isoformat(), offer.source, offer.marketing_carrier,
            offer.operating_carrier, offer.flight_number, offer.flight_key, offer.origin_airport,
            offer.destination_airport, offer.departure_local.isoformat(),
            offer.arrival_local.isoformat(), offer.timezone, offer.direct,
            str(offer.price), offer.currency, offer.seats_available,
            offer.search_id, offer.offer_id, confirmed,
            offer.baggage_category.value, offer.baggage,
        ) for offer in offers]
        if not values:
            return
        with self.db.transaction():
            with self.db.cursor() as cursor:
                cursor.executemany("""INSERT INTO offers (
                    search_run_id, checked_at_utc, source, marketing_carrier, operating_carrier,
                    flight_number, flight_key, origin_airport, destination_airport,
                    departure_local, arrival_local, timezone, direct, price, currency,
                    seats_available, search_id, offer_id, confirmed, baggage_category, baggage_code
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", values)

    def confirm_offer(self, run_id: int, offer: FlightOffer) -> None:
        cursor = self.db.execute("""UPDATE offers SET confirmed=TRUE WHERE search_run_id=%s
            AND flight_key=%s AND price=%s AND offer_id=%s""",
            (run_id, offer.flight_key, str(offer.price), offer.offer_id))
        if cursor.rowcount != 1:
            raise RuntimeError("Could not mark rechecked offer as confirmed")

    def confirm_run_offers(self, run_id: int) -> None:
        self.db.execute("UPDATE offers SET confirmed=TRUE WHERE search_run_id=%s", (run_id,))

    def historical_minimum(self) -> Decimal | None:
        rows = self.db.execute("SELECT price FROM offers WHERE confirmed=TRUE").fetchall()
        return min((Decimal(row["price"]) for row in rows), default=None)

    def previous_successful_check_minimum(self) -> Decimal | None:
        run = self.db.execute("""SELECT r.id FROM search_runs AS r
            LEFT JOIN search_runs AS next_run ON next_run.id =
                (SELECT MIN(id) FROM search_runs WHERE id > r.id)
            WHERE r.status IN ('baseline', 'observed', 'empty')
               OR (r.status = 'candidate' AND next_run.status IN ('confirmed', 'unconfirmed'))
            ORDER BY r.id DESC LIMIT 1""").fetchone()
        if run is None:
            return None
        rows = self.db.execute("SELECT price FROM offers WHERE search_run_id=%s", (run["id"],)).fetchall()
        return min((Decimal(row["price"]) for row in rows), default=None)

    def previous_successful_baggage_prices(self) -> dict[tuple[str, BaggageCategory], Decimal]:
        run = self.db.execute("""SELECT r.id FROM search_runs AS r
            WHERE r.status IN ('baseline', 'observed', 'candidate', 'confirmed', 'unconfirmed')
              AND EXISTS (SELECT 1 FROM offers WHERE search_run_id = r.id)
            ORDER BY r.id DESC LIMIT 1""").fetchone()
        if run is None:
            return {}
        rows = self.db.execute("""SELECT flight_key,baggage_category,price
            FROM offers WHERE search_run_id=%s""", (run["id"],)).fetchall()
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
            previous_price,status,error_message) VALUES(%s,%s,%s,%s,%s,%s,%s)""",
            (datetime.now(timezone.utc).isoformat(), kind, offer.flight_key, str(offer.price),
             str(previous) if previous is not None else None, status, error))

    def recent_runs(self, limit: int = 10):
        return self.db.execute("""SELECT r.id,r.started_at,r.status,r.error_message,
            COUNT(o.id) AS offers_count FROM search_runs r
            LEFT JOIN offers o ON o.search_run_id=r.id
            GROUP BY r.id ORDER BY r.id DESC LIMIT %s""", (limit,)).fetchall()

    def recent_offers(self, limit: int = 15):
        return self.db.execute("""SELECT checked_at_utc,flight_number,operating_carrier,
            origin_airport,destination_airport,departure_local,price,currency,confirmed
            FROM offers ORDER BY id DESC LIMIT %s""", (limit,)).fetchall()
