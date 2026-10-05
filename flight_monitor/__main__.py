from __future__ import annotations

import argparse
import logging
import sys

from .config import Config
from .bot import run_bot
from .check_runner import run_scheduled_check
from .notifier import TelegramNotifier
from .presentation import flight_table_lines, format_price, group_physical_flights
from .storage import open_repository


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="OneTwoTrip flight price monitor")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="perform one fresh search")
    check.add_argument("--dry-run", action="store_true", help="do not send Telegram")
    commands.add_parser("history", help="show recent saved searches and offers")
    commands.add_parser("telegram-test", help="send one test message to Telegram")
    commands.add_parser("bot", help="receive Telegram updates by long polling")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.from_env()
        if args.command == "telegram-test":
            notifier = TelegramNotifier(config.telegram_bot_token,
                                        getattr(config, "owner_chat_id", None) or config.telegram_chat_id)
            if not notifier.enabled:
                logging.error("Telegram is not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
                              "(or TELEGRAM_OWNER_CHAT_ID).")
                return 1
            notifier.send("Flight Monitor: Telegram работает")
            print("Telegram test message sent.")
            return 0
        if args.command == "bot":
            run_bot(config)
            return 0
        if args.command == "history":
            with open_repository(config) as repo:
                print("Confirmed historical minimum:", repo.historical_minimum())
                print("Recent runs:")
                for run in repo.recent_runs():
                    print(f"  #{run['id']} {run['started_at']} {run['status']} offers={run['offers_count']}")
                print("Recent offers:")
                for offer in repo.recent_offers():
                    print(f"  {offer['checked_at_utc']} {offer['flight_number']} "
                          f"{offer['origin_airport']}->{offer['destination_airport']} "
                          f"{offer['departure_local']} {offer['price']} {offer['currency']} "
                          f"confirmed={bool(offer['confirmed'])}")
            return 0
        result = run_scheduled_check(config, dry_run=args.dry_run)
        if result is None:
            print("Статус: skipped_locked")
            return 0
        print(f"Предложений сохранено: {len(result.offers)}; физических рейсов: "
              f"{len(group_physical_flights(result.offers))}")
        print("10 самых дешёвых физических рейсов:")
        for line in flight_table_lines(result.offers):
            print(line)
        print(f"Статус: {result.status}")
        print(f"Минимальная цена сейчас: "
              f"{format_price(result.current_minimum.price) if result.current_minimum else '—'}")
        print(f"Подтверждённый исторический минимум: "
              f"{format_price(result.historical_after) if result.historical_after is not None else '—'}")
        return 0
    except Exception as exc:
        if args.command == "telegram-test":
            logging.error("Telegram test failed. Check Telegram configuration and connection.")
        else:
            logging.error("Check failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
