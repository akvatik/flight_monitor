from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class BaggageCategory(StrEnum):
    WITHOUT_BAGGAGE = "WITHOUT_BAGGAGE"
    WITH_BAGGAGE = "WITH_BAGGAGE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FlightOffer:
    checked_at_utc: datetime
    source: str
    marketing_carrier: str
    operating_carrier: str | None
    flight_number: str
    origin_airport: str
    destination_airport: str
    departure_local: datetime
    arrival_local: datetime
    timezone: str
    direct: bool
    price: Decimal
    currency: str
    seats_available: int | None
    search_id: str | None
    offer_id: str | None
    fare_name: str | None = None
    baggage: str | None = None
    seller: str | None = None
    baggage_category: BaggageCategory = BaggageCategory.UNKNOWN

    @property
    def flight_key(self) -> str:
        # Offer and search IDs are temporary. This key survives a new search.
        operator = self.operating_carrier or self.marketing_carrier
        return "|".join((operator, self.origin_airport, self.destination_airport,
                         self.departure_local.isoformat(), self.arrival_local.isoformat()))


def minimum(offers: list[FlightOffer]) -> FlightOffer | None:
    return min(offers, key=lambda offer: offer.price, default=None)
