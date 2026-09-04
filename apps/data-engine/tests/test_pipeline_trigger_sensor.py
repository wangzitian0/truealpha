"""#495 (3/3): DB-mediated manual trigger — migration 0034 + the Dagster sensor.

Proves the whole Postgres-mediated handshake (init.md §2.2) without any
network path between app and daemon:

  * app_runtime may INSERT a request and read it back, cannot UPDATE it;
  * the sensor turns a pending row into exactly one RunRequest carrying the
    requested executed_at, and marks the row consumed with the run_key;
  * a second evaluation yields nothing (consume-once);
  * consumed rows are immutable and nothing can DELETE the audit trail.

Skips without a local Postgres; ci-python/ci-db run it migrated.
"""

import os
from datetime import UTC, datetime

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.dagster_defs import pipeline_trigger_sensor

_EXECUTED_AT = datetime(2026, 7, 27, 22, 15, tzinfo=UTC)


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def _insert_request(connection, dedupe_key: str) -> int:
    return connection.execute(
        "insert into staging.pipeline_trigger_requests (job_name, executed_at, requested_by, dedupe_key) "
        "values ('topt_live_pipeline', %s, 'principal:owner', %s) returning request_id",
        (_EXECUTED_AT, dedupe_key),
    ).fetchone()[0]


def test_app_runtime_can_insert_and_read_but_not_update(connection) -> None:
    connection.execute("set local role app_runtime")
    request_id = _insert_request(connection, "test-grant-shape")
    assert connection.execute(
        "select consumed_at from staging.pipeline_trigger_requests where request_id = %s", (request_id,)
    ).fetchone() == (None,)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        connection.execute(
            "update staging.pipeline_trigger_requests set launched_run_key = 'forged' where request_id = %s",
            (request_id,),
        )


def test_sensor_launches_once_with_requested_executed_at_and_consumes(connection, monkeypatch) -> None:
    # dedupe_key must be fresh per test run: the row below is COMMITTED (the
    # sensor opens its own connection) and the table is append-only, so a
    # reused key would collide with the previous run's audit row.
    import uuid

    dedupe_key = f"test-sensor-{uuid.uuid4().hex[:12]}"
    request_id = _insert_request(connection, dedupe_key)
    connection.commit()  # the sensor opens its own connection and must see the row
    try:
        monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _reuse(connection))
        context = dg.build_sensor_context()

        requests = list(pipeline_trigger_sensor(context))
        assert len(requests) == 1
        assert requests[0].run_key == f"manual:{dedupe_key}"
        tick_config = requests[0].run_config["ops"]["run_topt_live_tick"]["config"]
        assert tick_config["executed_at"] == _EXECUTED_AT.isoformat()

        consumed_at, run_key = connection.execute(
            "select consumed_at, launched_run_key from staging.pipeline_trigger_requests where request_id = %s",
            (request_id,),
        ).fetchone()
        assert consumed_at is not None and run_key == f"manual:{dedupe_key}"

        assert list(pipeline_trigger_sensor(dg.build_sensor_context())) == []

        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "update staging.pipeline_trigger_requests set launched_run_key = 'again' where request_id = %s",
                (request_id,),
            )
    finally:
        connection.rollback()
        _cleanup(connection)


def test_delete_is_rejected_even_for_the_owner_role(connection) -> None:
    request_id = _insert_request(connection, "test-no-delete")
    with pytest.raises(psycopg.errors.CheckViolation):
        connection.execute("delete from staging.pipeline_trigger_requests where request_id = %s", (request_id,))


class _reuse:
    """Context manager handing the sensor the test's own connection so its
    reads/writes stay inside this test's transaction... except the fixture
    row above is committed, so `_cleanup` removes it afterwards."""

    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        return self._connection

    def __exit__(self, *exc):
        return False


def _cleanup(connection) -> None:
    # The table rejects DELETE by trigger; committed test rows are neutralized
    # by consuming them (if the sensor has not already) so reruns stay clean —
    # dedupe_key is unique, so a NEW key per test run is the real isolation.
    connection.execute(
        "update staging.pipeline_trigger_requests set consumed_at = clock_timestamp(), "
        "launched_run_key = 'test-cleanup' where dedupe_key like 'test-sensor-%' and consumed_at is null"
    )
    connection.commit()


@pytest.mark.parametrize(
    "tick", [t for t in __import__("data_engine.lanes.capture", fromlist=["TICKS"]).TICKS], ids=lambda t: t.key
)
def test_sensor_dispatches_every_declared_tick_to_its_own_op(connection, monkeypatch, tick) -> None:
    """#72 scope 4: dispatch is derived from the declarations, so a new universe is
    triggerable without editing the sensor. Each declared job name must launch its
    own job with the config keyed by its own op name."""
    import uuid

    dedupe_key = f"test-tick-{tick.key}-{uuid.uuid4().hex[:12]}"
    request_id = connection.execute(
        "insert into staging.pipeline_trigger_requests (job_name, executed_at, requested_by, dedupe_key) "
        "values (%s, %s, %s, %s) returning request_id",
        (tick.job_name, _EXECUTED_AT, "test", dedupe_key),
    ).fetchone()[0]
    connection.commit()
    try:
        monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _reuse(connection))
        requests = list(pipeline_trigger_sensor(dg.build_sensor_context()))
        assert len(requests) == 1 and requests[0].job_name == tick.job_name
        assert set(requests[0].run_config["ops"]) == {tick.op_name}
        assert requests[0].run_config["ops"][tick.op_name]["config"]["executed_at"] == _EXECUTED_AT.isoformat()
        assert request_id is not None
    finally:
        connection.rollback()
        _cleanup(connection)
