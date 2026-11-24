"""The one way data-engine opens a Postgres connection.

Every module of `src` opens its connection here. This function reads `settings.database_url`
and sets `connect_timeout`, so a database that does not answer fails after
`settings.database_connect_timeout_seconds` instead of waiting without a limit.
`tests/test_database_connect_entry.py` fails when a module opens a connection another way.
"""

import psycopg

from data_engine.config import settings


def connect(*, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(
        settings.database_url,
        connect_timeout=settings.database_connect_timeout_seconds,
        autocommit=autocommit,
    )
