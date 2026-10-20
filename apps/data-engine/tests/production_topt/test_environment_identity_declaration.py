"""A capture tick declares its environment and holds no row write when nothing changes (#1412).

`plan_and_persist` runs `ensure_environment_identity` as its first statement. The tick runs in
one transaction and commits about 14 minutes later. Before #1412 the declaration wrote the
`mart.environment_identity` row on every tick, so every tick held that row write until its
first commit. On 2026-10-15 a boot replay waited for it, hit lock_timeout, and the staging llm
container restarted 7 times. A second tick that started meanwhile waited for it too.

Each test uses a database of its own, because a test commits and holds a second connection.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import psycopg
import pytest
from data_engine.datahub.production_topt.composition import ensure_environment_identity
from truealpha_contracts.common import CaptureEnvironment
from truealpha_runtime.testing import isolated_test_database


@pytest.fixture
def database() -> Iterator[str]:
    with isolated_test_database("envdecl") as url:
        yield url


def _commit_environment(database: str, environment: str) -> None:
    with psycopg.connect(database, autocommit=True) as connection:
        connection.execute("update mart.environment_identity set environment = %s", (environment,))


def _declared(database: str) -> list[tuple[str, datetime]]:
    with psycopg.connect(database) as connection:
        return connection.execute("select environment, declared_at from mart.environment_identity").fetchall()


@pytest.mark.parametrize("environment", [CaptureEnvironment.STAGING, CaptureEnvironment.PRODUCTION])
def test_a_tick_that_declares_the_current_environment_holds_no_row(
    database: str, environment: CaptureEnvironment
) -> None:
    _commit_environment(database, environment.value)
    before = _declared(database)
    assert [row[0] for row in before] == [environment.value]
    with psycopg.connect(database) as tick, psycopg.connect(database, autocommit=True) as boot:
        ensure_environment_identity(tick, environment)  # the tick's transaction stays open
        # A row that an open transaction wrote refuses this lock at once (55P03).
        boot.execute("select 1 from mart.environment_identity for update nowait")
        tick.commit()
    assert _declared(database) == before


def test_a_tick_that_declares_another_environment_writes_it(database: str) -> None:
    _commit_environment(database, CaptureEnvironment.PRODUCTION.value)
    ((_, declared_before),) = _declared(database)
    with psycopg.connect(database) as tick:
        ensure_environment_identity(tick, CaptureEnvironment.STAGING)
        tick.commit()
    ((environment, declared_at),) = _declared(database)
    assert environment == "staging"
    assert declared_at > declared_before


def test_a_tick_declares_the_environment_when_the_table_is_empty(database: str) -> None:
    with psycopg.connect(database, autocommit=True) as connection:
        connection.execute("delete from mart.environment_identity")
    with psycopg.connect(database) as tick:
        ensure_environment_identity(tick, "staging")
        tick.commit()
    assert [row[0] for row in _declared(database)] == ["staging"]
