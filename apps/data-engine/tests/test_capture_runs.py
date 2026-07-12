import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from data_engine.capture import repository, runs
from data_engine.capture.topt import build_topt_scope
from data_engine.config import settings
from truealpha_runtime.testing import skip_or_fail

psycopg = pytest.importorskip("psycopg")


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(settings.database_url, connect_timeout=3)
    except psycopg.OperationalError:
        skip_or_fail("no reachable Postgres (make runtime-up && make db-migrate)")
    yield connection
    connection.rollback()
    connection.close()


def test_capture_run_binding_is_idempotent_but_cannot_be_rebound(conn):
    scope = build_topt_scope()
    repository.put_scope(conn, scope)
    binding = runs.CaptureRunBinding(
        run_id=f"run:{uuid.uuid4().hex}",
        capture_scope_id=scope.capture_scope_id,
        release_manifest_id="release-manifest:" + "a" * 64,
        image_digest="sha256:" + "b" * 64,
        configuration_sha256="c" * 64,
        schedule_name="topt_staging_daily_schedule",
        started_at=datetime.now(UTC),
    )
    assert runs.put(conn, scope=scope, binding=binding)
    assert not runs.put(conn, scope=scope, binding=binding)
    assert runs.get(conn, binding.run_id) == binding

    with pytest.raises(ValueError, match="different immutable inputs"):
        runs.put(
            conn,
            scope=scope,
            binding=replace(binding, configuration_sha256="d" * 64),
        )
