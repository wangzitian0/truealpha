"""Warehouse connection (settings.database_url). One place to connect so scripts
don't each parse env config; callers own transaction boundaries."""

import psycopg

from data_engine.config import settings


def connect() -> psycopg.Connection:
    return psycopg.connect(settings.database_url)
