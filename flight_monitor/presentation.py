from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from html import escape
import re
from zoneinfo import ZoneInfo

from .models import BaggageCategory, FlightOffer

AIRLINE_NAMES = {
    "SU": "Аэрофлот", "FV": "Россия", "S7": "S7 Airlines",
    "DP": "Победа", "A4": "Азимут",
}
MAX_RICH_MESSAGE_LENGTH = 32768
MAIN_HEADERS = ("", "Рейс", "Авиакомпания", "Маршрут", "Вылет", "Прилёт", "Без багажа", "С багажом")
MAIN_WIDTHS = (4, 9, 31, 17, 11, 11, 12, 12)


@dataclass(frozen=True)
class FlightPrices:
    offer: FlightOffer
    without_baggage: Decimal | None
    with_baggage: Decimal | None

    @property
    def flight_key(self) -> str:
        return self.offer.flight_key


class RichMarkdown(str):
    """Report content routed to sendRichMessage by the Telegram notifier."""


def group_physical_flights(offers: list[FlightOffer]) -> list[FlightPrices]:
    grouped: dict[str, FlightPrices] = {}
    for offer in offers:
        current = grouped.get(offer.flight_key)
        representative = offer if current is None or offer.price < current.offer.price else current.offer
        without = current.without_baggage if current else None
        with_baggage = current.with_baggage if current else None
        if offer.baggage_category == BaggageCategory.WITHOUT_BAGGAGE:
            without = offer.price if without is None else min(without, offer.price)
        elif offer.baggage_category == BaggageCategory.WITH_BAGGAGE:
            with_baggage = offer.price if with_baggage is None else min(with_baggage, offer.price)
        grouped[offer.flight_key] = FlightPrices(representative, without, with_baggage)
    return list(grouped.values())


def _sort_key(flight: FlightPrices) -> tuple:
    return (flight.without_baggage is None,
            flight.without_baggage if flight.without_baggage is not None
            else flight.with_baggage if flight.with_baggage is not None else flight.offer.price,
            flight.offer.departure_local, flight.offer.flight_number)


def category_leader(flights: list[FlightPrices], category: BaggageCategory) -> FlightPrices | None:
    priced = [flight for flight in flights if (
        flight.without_baggage if category == BaggageCategory.WITHOUT_BAGGAGE
        else flight.with_baggage) is not None]
    if not priced:
        return None
    return min(priced, key=lambda flight: (
        flight.without_baggage if category == BaggageCategory.WITHOUT_BAGGAGE else flight.with_baggage,
        flight.offer.departure_local, flight.offer.flight_number))


def select_main_flights(flights: list[FlightPrices], limit: int = 10) -> list[FlightPrices]:
    ordered = sorted(flights, key=_sort_key)
    selected = ordered[:limit]
    leaders = [category_leader(flights, category) for category in (
        BaggageCategory.WITHOUT_BAGGAGE, BaggageCategory.WITH_BAGGAGE)]
    leader_keys = {leader.flight_key for leader in leaders if leader is not None}
    for leader in leaders:
        if leader is None or any(item.flight_key == leader.flight_key for item in selected):
            continue
        if len(selected) < limit:
            selected.append(leader)
        else:
            replace_at = next((index for index in range(len(selected) - 1, -1, -1)
                               if selected[index].flight_key not in leader_keys), None)
            if replace_at is not None:
                selected[replace_at] = leader
    return sorted(selected, key=_sort_key)


def cheapest_physical_flights(offers: list[FlightOffer], limit: int | None = 10) -> list[FlightOffer]:
    flights = group_physical_flights(offers)
    selected = sorted(flights, key=_sort_key) if limit is None else select_main_flights(flights, limit)
    return [flight.offer for flight in selected]


def airline_name(code: str) -> str:
    return AIRLINE_NAMES.get(code, code)


def display_airline(offer: FlightOffer) -> str:
    marketing = airline_name(offer.marketing_carrier)
    #if offer.operating_carrier and offer.operating_carrier != offer.marketing_carrier:
        #return f"{marketing} · выполняет {airline_name(offer.operating_carrier)}"
    return marketing


def format_local_time(value: datetime) -> str:
    return value.strftime("%d.%m %H:%M")


def format_price(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{rounded:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def rounded_rubles(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_rubles(value: Decimal | None) -> str:
    return "—" if value is None else f"{rounded_rubles(value):,}".replace(",", " ") + " ₽"


def _clip(value: str, limit: int) -> str:
    value = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _table_lines(headers: tuple[str, ...], widths: tuple[int, ...], rows: list[tuple[str, ...]]) -> list[str]:
    def line(values: tuple[str, ...]) -> str:
        return " | ".join(_clip(value, width).ljust(width) for value, width in zip(values, widths)).rstrip()
    if not rows:
        return ["Рейсы не найдены."]
    return [line(headers), "-+-".join("-" * width for width in widths)] + [line(row) for row in rows]


def _markers(flight: FlightPrices, without_leader: FlightPrices | None,
             with_leader: FlightPrices | None) -> str:
    return ("✈️" if without_leader and flight.flight_key == without_leader.flight_key else "") + (
        "🧳" if with_leader and flight.flight_key == with_leader.flight_key else "")


def _main_rows(flights: list[FlightPrices], without_leader: FlightPrices | None,
               with_leader: FlightPrices | None) -> list[tuple[str, ...]]:
    return [(_markers(flight, without_leader, with_leader), flight.offer.flight_number,
             display_airline(flight.offer),
             f"{flight.offer.origin_airport}→{flight.offer.destination_airport}",
             format_local_time(flight.offer.departure_local), format_local_time(flight.offer.arrival_local),
             format_rubles(flight.without_baggage), format_rubles(flight.with_baggage))
            for flight in flights]


def flight_table_lines(offers: list[FlightOffer]) -> list[str]:
    flights = group_physical_flights(offers)
    selected = select_main_flights(flights)
    return _table_lines(MAIN_HEADERS, MAIN_WIDTHS,
                        _main_rows(selected,
                                   category_leader(flights, BaggageCategory.WITHOUT_BAGGAGE),
                                   category_leader(flights, BaggageCategory.WITH_BAGGAGE)))


def time_window_top_three(flights: list[FlightPrices], start_hour: int, end_hour: int) -> list[FlightPrices]:
    return sorted((flight for flight in flights
                   if flight.without_baggage is not None
                   and start_hour <= flight.offer.departure_local.hour < end_hour),
                  key=_sort_key)[:3]


def markdown_table_cell(value: str) -> str:
    """Escape only dynamic cell content, preserving the report's Markdown syntax."""
    clean = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return re.sub(r"([\\|*_`\[\]~!])", r"\\\1", escape(clean, quote=False))


def _markdown_table(headers: tuple[str, ...], rows: list[tuple[str, ...]],
                    right_align: frozenset[int] = frozenset()) -> str:
    header = "| " + " | ".join(headers) + " |"
    separator = "|" + "|".join("---:" if index in right_align else "---"
                              for index in range(len(headers))) + "|"
    body = ["| " + " | ".join(row) + " |" for row in rows]
    table = "\n".join([header, separator, *body])
    return table if rows else table + "\n\nРейсы не найдены."


def _rich_price(value: Decimal | None, *, bold: bool = False) -> str:
    text = markdown_table_cell(format_rubles(value))
    return f"**{text}**" if bold and value is not None else text


def _main_markdown_rows(flights: list[FlightPrices], without_leader: FlightPrices | None,
                        with_leader: FlightPrices | None) -> list[tuple[str, ...]]:
    rows = []
    for flight in flights:
        offer = flight.offer
        without_winner = without_leader is not None and flight.flight_key == without_leader.flight_key
        with_winner = with_leader is not None and flight.flight_key == with_leader.flight_key
        rows.append((
            _markers(flight, without_leader, with_leader),
            markdown_table_cell(offer.flight_number),
            markdown_table_cell(display_airline(offer)),
            markdown_table_cell(f"{offer.origin_airport} → {offer.destination_airport}"),
            markdown_table_cell(format_local_time(offer.departure_local)),
            markdown_table_cell(format_local_time(offer.arrival_local)),
            _rich_price(flight.without_baggage, bold=without_winner),
            _rich_price(flight.with_baggage, bold=with_winner),
        ))
    return rows


def _time_markdown_rows(flights: list[FlightPrices]) -> list[tuple[str, ...]]:
    return [(
        markdown_table_cell(flight.offer.flight_number),
        markdown_table_cell(display_airline(flight.offer)),
        markdown_table_cell(format_local_time(flight.offer.departure_local)),
        markdown_table_cell(format_local_time(flight.offer.arrival_local)),
        _rich_price(flight.without_baggage),
        _rich_price(flight.with_baggage),
    ) for flight in flights]


def _delta(current: Decimal | None, previous: Decimal | None) -> str:
    if current is None or previous is None:
        return "нет данных"
    difference = rounded_rubles(current) - rounded_rubles(previous)
    if difference < 0:
        return f"−{abs(difference):,} ₽".replace(",", " ")
    if difference > 0:
        return f"+{difference:,} ₽".replace(",", " ")
    return "без изменений"


def _winner_delta(current: Decimal | None, previous: Decimal | None) -> str:
    difference = _delta(current, previous)
    if difference.startswith("−"):
        return "🟢 " + difference
    if difference.startswith("+"):
        return "🔴 " + difference
    return "⚪ " + difference


def _leader_block(title: str, flight: FlightPrices | None, category: BaggageCategory,
                  previous: dict[tuple[str, BaggageCategory], Decimal]) -> str:
    if flight is None:
        return title + "\nНет данных"
    offer = flight.offer
    identity = markdown_table_cell(_clip(f"{offer.flight_number} · {display_airline(offer)}", 200))
    old_without = previous.get((flight.flight_key, BaggageCategory.WITHOUT_BAGGAGE))
    old_with = previous.get((flight.flight_key, BaggageCategory.WITH_BAGGAGE))
    if category == BaggageCategory.WITHOUT_BAGGAGE:
        rows = [f"**Без багажа: {format_rubles(flight.without_baggage)} "
                f"{_winner_delta(flight.without_baggage, old_without)}**",
                f"С багажом: {format_rubles(flight.with_baggage)} · "
                f"{_delta(flight.with_baggage, old_with)}"]
    else:
        rows = [f"**С багажом: {format_rubles(flight.with_baggage)} "
                f"{_winner_delta(flight.with_baggage, old_with)}**",
                f"Без багажа: {format_rubles(flight.without_baggage)} · "
                f"{_delta(flight.without_baggage, old_without)}"]
    return title + "\n\n**" + identity + "**\n\n" + "\n\n".join(rows)


def telegram_summary_messages(
    offers: list[FlightOffer],
    previous_prices: dict[tuple[str, BaggageCategory], Decimal] | None = None,
    *, checked_at_utc: datetime | None = None,
) -> list[RichMarkdown]:
    flights = group_physical_flights(offers)
    without_leader = category_leader(flights, BaggageCategory.WITHOUT_BAGGAGE)
    with_leader = category_leader(flights, BaggageCategory.WITH_BAGGAGE)
    checked = checked_at_utc or (offers[0].checked_at_utc if offers else datetime.now(timezone.utc))
    updated = checked.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m %H:%M")
    heading = f"# ✈️ Текущие цены на авиабилеты\n\n**Обновлено:** {updated}"
    main = "## 🔝 10 самых дешёвых рейсов\n\n" + _markdown_table(
        MAIN_HEADERS, _main_markdown_rows(select_main_flights(flights), without_leader, with_leader),
        frozenset({6, 7}))
    morning = "## 🌅 Топ-3: вылет с 08:00 до 16:00\n\n" + _markdown_table(
        ("Рейс", "Авиакомпания", "Вылет", "Прилёт", "Без багажа", "С багажом"),
        _time_markdown_rows(time_window_top_three(flights, 8, 16)), frozenset({4, 5}))
    evening = "## 🌆 Топ-3: вылет с 16:00 до 00:00\n\n" + _markdown_table(
        ("Рейс", "Авиакомпания", "Вылет", "Прилёт", "Без багажа", "С багажом"),
        _time_markdown_rows(time_window_top_three(flights, 16, 24)), frozenset({4, 5}))
    previous = previous_prices or {}
    leaders = "---\n\n" + _leader_block("## ✈️ Самый дешёвый билет без багажа", without_leader,
                                             BaggageCategory.WITHOUT_BAGGAGE, previous)
    leaders += "\n\n" + _leader_block("## 🧳 Самый дешёвый билет с багажом", with_leader,
                                        BaggageCategory.WITH_BAGGAGE, previous)
    groups = [heading + "\n\n" + main, morning + "\n\n" + evening, leaders]
    complete = "\n\n".join(groups)
    if len(complete.encode("utf-8")) <= MAX_RICH_MESSAGE_LENGTH:
        return [RichMarkdown(complete)]
    if any(len(group.encode("utf-8")) > MAX_RICH_MESSAGE_LENGTH for group in groups):
        raise ValueError("A report section is too long for Telegram Rich Message")
    return [RichMarkdown(group) for group in groups]
