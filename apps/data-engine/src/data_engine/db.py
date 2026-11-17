"""Postgres connection helper over `settings.database_url`."""

import psycopg

from data_engine.config import settings


def connect() -> psycopg.Connection:
    return psycopg.connect(
        settings.database_url,
        connect_timeout=settings.database_connect_timeout_seconds,
    )
