from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import BaggageCategory, FlightOffer

LOG = logging.getLogger(__name__)
MOSCOW_AIRPORTS = frozenset({"SVO", "VKO", "DME", "ZIA"})
POSITIVE_BAGGAGE_CODE = re.compile(r"[1-9][0-9]*(?:PC|K|KG)\Z")


class SchemaError(ValueError):
    pass


def registered_baggage(variant: dict[str, Any], services: dict[str, Any]) -> tuple[BaggageCategory, str | None]:
    ids = variant.get("serviceIds")
    if not isinstance(ids, list) or not ids:
        return BaggageCategory.UNKNOWN, None
    baggage_codes: list[str] = []
    for service_id in ids:
        service = services.get(service_id)
        if not isinstance(service, dict):
            return BaggageCategory.UNKNOWN, None
        if service.get("type") == "baggage":
            code = service.get("code")
            if not isinstance(code, str):
                return BaggageCategory.UNKNOWN, None
            baggage_codes.append(code.strip().upper())
    if len(baggage_codes) != 1:
        return BaggageCategory.UNKNOWN, None
    code = baggage_codes[0]
    if code == "0PC":
        return BaggageCategory.WITHOUT_BAGGAGE, code
    if POSITIVE_BAGGAGE_CODE.fullmatch(code):
        return BaggageCategory.WITH_BAGGAGE, code
    return BaggageCategory.UNKNOWN, code or None


def parse_offers(raw: dict[str, Any], *, origin: str, destination: str,
                 departure_date: date, adults: int, cabin_class: str,
                 direct_only: bool, checked_at_utc: datetime | None = None) -> list[FlightOffer]:
    if not isinstance(raw, dict) or raw.get("version") != 3:
        raise SchemaError("Unsupported OneTwoTrip response version")
    info = raw.get("requestInfo")
    if not isinstance(info, dict):
        raise SchemaError("Missing requestInfo")
    directions = info.get("directions")
    passengers = info.get("passengerCount")
    if not isinstance(directions, list) or len(directions) != 1 or not isinstance(passengers, dict):
        raise SchemaError("Unexpected route or passenger schema")
    direction = directions[0]
    if (direction.get("from"), direction.get("to"), direction.get("date")) != (
            origin, destination, departure_date.isoformat()):
        raise SchemaError("Response route/date does not match request")
    if (passengers.get("adult"), passengers.get("child"), passengers.get("infant")) != (adults, 0, 0):
        raise SchemaError("Response passenger count does not match request")
    if cabin_class != "E" or not direct_only:
        raise ValueError("This source supports economy direct-only monitoring")
    currency = raw.get("currency")
    if currency != "RUB":
        raise SchemaError("Unexpected or missing currency")
    if raw.get("fromCache") is not False:
        raise SchemaError("Search response is cached or cache status is unknown")
    trips, variants, prices = (raw.get(k) for k in ("trips", "transportationVariants", "prices"))
    if not all(isinstance(x, dict) for x in (trips, variants, prices)):
        raise SchemaError("Missing trips, transportationVariants or prices")
    references = raw.get("references")
    services = references.get("services") if isinstance(references, dict) else None
    if not isinstance(services, dict):
        services = {}
    LOG.info("JSON contains %d trips, %d variants, %d prices", len(trips), len(variants), len(prices))
    checked = checked_at_utc or datetime.now(timezone.utc)
    results: list[FlightOffer] = []
    warnings = 0
    for price_id, price_obj in prices.items():
        if not isinstance(price_obj, dict):
            warnings += 1
            continue
        ids = price_obj.get("transportationVariantIds")
        if not isinstance(ids, list) or len(ids) != 1:
            warnings += 1
            continue
        variant = variants.get(ids[0])
        if not isinstance(variant, dict):
            warnings += 1
            continue
        refs = variant.get("tripRefs")
        if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], dict):
            warnings += 1
            continue
        if refs[0].get("serviceClass") != "E":
            continue
        trip = trips.get(refs[0].get("tripId"))
        if not isinstance(trip, dict):
            warnings += 1
            continue
        if trip.get("transportType") != "plane" or price_obj.get("transportationType") != "plane":
            continue
        if trip.get("from") != origin:
            continue
        to = trip.get("to")
        if destination == "MOW" and to not in MOSCOW_AIRPORTS:
            continue
        if "stops" in trip and trip["stops"] != []:
            continue
        carrier = trip.get("carrier")
        operator = trip.get("operatingCarrier")
        number = trip.get("carrierTripNumber")
        if not isinstance(carrier, str) or not isinstance(number, str) or not number:
            warnings += 1
            continue
        try:
            depart = datetime.fromisoformat(trip["startDateTime"])
            arrive = datetime.fromisoformat(trip["endDateTime"])
            amount = Decimal(str(price_obj["totalAmount"]))
            seats = price_obj.get("seatsAvailable")
            if depart.date() != departure_date or arrive <= depart or amount <= 0:
                continue
            if depart.tzinfo or arrive.tzinfo or not amount.is_finite():
                warnings += 1
                continue
            if seats is not None and (not isinstance(seats, int) or seats <= 0):
                continue
        except (KeyError, ValueError, TypeError, InvalidOperation):
            warnings += 1
            continue
        baggage_category, baggage_code = registered_baggage(variant, services)
        results.append(FlightOffer(
            checked_at_utc=checked, source="onetwotrip", marketing_carrier=carrier,
            operating_carrier=operator if isinstance(operator, str) else None,
            flight_number=f"{carrier} {number}", origin_airport=origin,
            destination_airport=to, departure_local=depart, arrival_local=arrive,
            timezone="Europe/Moscow", direct=True, price=amount, currency=currency,
            seats_available=seats, search_id=str(raw.get("id")) if raw.get("id") else None,
            offer_id=str(price_obj.get("id", price_id)),
            baggage=baggage_code, baggage_category=baggage_category,
        ))
    if warnings:
        LOG.warning("Skipped %d ambiguous or malformed price records", warnings)
    LOG.info("Normalized %d direct offers", len(results))
    return results
