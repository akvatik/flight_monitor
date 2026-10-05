"""Choose a repository without changing the monitoring service."""

from __future__ import annotations

from .config import Config
from .repository import PriceRepository


def open_repository(config: Config):
    if config.database_url and config.database_url.startswith(("postgresql://", "postgres://")):
        from .postgres_repository import PostgresPriceRepository
        return PostgresPriceRepository(config.database_url)
    return PriceRepository(config.database_path)
