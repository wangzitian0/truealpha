"""Mutation check and invariants for test DB write guards (#1197).

Acceptance:
1. A DB-backed test writes only when CI=true or ALLOW_CLEAR_TEST=1.
   Otherwise it skips with a visible warning.
2. A test never defaults DATABASE_URL to port 5432 for writes.
3. Mutation check: With the guard removed and env unset, a write test must fail
   against a closed port. With the guard in place, the same run skips and makes
   no connection.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import psycopg
import pytest
from truealpha_runtime.testing import (
    GATE_MARKER,
    can_write_test_database,
    db_writes_allowed,
    guard_db_write,
    isolated_test_database,
)


@pytest.fixture(autouse=True)
def _isolate_guard_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate tests from external CI runtime flags to test skip semantics cleanly."""
    monkeypatch.delenv("TRUEALPHA_REQUIRE_RUNTIME", raising=False)


def test_guard_skips_when_write_not_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    """When CI and ALLOW_CLEAR_TEST are unset, guard_db_write must skip and make zero connections."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("ALLOW_CLEAR_TEST", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert not can_write_test_database()
    assert not db_writes_allowed()

    with patch("psycopg.connect") as mock_connect:
        with pytest.warns(UserWarning, match="protect ambient databases"):
            with pytest.raises(pytest.skip.Exception, match="protect ambient databases"):
                guard_db_write()
        mock_connect.assert_not_called()


def test_guard_rejects_unset_database_url_when_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    """When write is authorized but DATABASE_URL is unset, it must not default to port 5432."""
    monkeypatch.setenv("ALLOW_CLEAR_TEST", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert can_write_test_database()
    assert not db_writes_allowed()

    with patch("psycopg.connect") as mock_connect:
        with pytest.warns(UserWarning, match="no default to port 5432"):
            with pytest.raises(pytest.skip.Exception, match="no default to port 5432"):
                guard_db_write()
        mock_connect.assert_not_called()


def test_guard_passes_explicit_database_url_when_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    """When authorized and explicit DATABASE_URL is provided, guard returns it."""
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@127.0.0.1:54329/testdb")

    assert db_writes_allowed()
    url = guard_db_write()
    assert url == "postgresql://user:pass@127.0.0.1:54329/testdb"


def test_root_gate_refuses_unprotected_psycopg_connect_and_fails_when_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance (#1197): a test that calls psycopg.connect without the guard is refused.

    Proves the root conftest gate is installed at import time and fails if removed.
    """
    assert getattr(psycopg.connect, GATE_MARKER, False) is True, (
        "root conftest.py must install the psycopg.connect write gate at import time"
    )
    real_connect = getattr(psycopg.connect, "_real_connect")
    assert callable(real_connect)

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("ALLOW_CLEAR_TEST", raising=False)
    closed_port_url = "postgresql://postgres:x@127.0.0.1:1/probe"
    monkeypatch.setenv("DATABASE_URL", closed_port_url)

    # 1. With the root gate in place: direct psycopg.connect skips with warning and makes 0 calls
    with pytest.warns(UserWarning, match="DB-backed test skipped: set CI=true or ALLOW_CLEAR_TEST=1"):
        with pytest.raises(
            pytest.skip.Exception,
            match="DB-backed test skipped: set CI=true or ALLOW_CLEAR_TEST=1 and DATABASE_URL to allow database access",
        ):
            psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=1)

    # 2. Mutation check: with the root gate removed, the same call dials the closed port and raises OperationalError
    monkeypatch.setattr(psycopg, "connect", real_connect)
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=1)


def test_mutation_check_unprotected_write_fails_against_closed_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation check (Acceptance #3):

    - With the guard in place and env unset, isolated_test_database skips without connecting.
    - With the guard bypassed, attempting connection against a closed port
      (127.0.0.1:1) raises psycopg.OperationalError.
    """
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("ALLOW_CLEAR_TEST", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    # 1. With guard in place: isolated_test_database skips cleanly
    with patch("psycopg.connect") as mock_connect:
        with pytest.warns(UserWarning, match="protect ambient databases"):
            with pytest.raises(pytest.skip.Exception):
                with isolated_test_database("mutation_check"):
                    pass
        mock_connect.assert_not_called()

    # 2. With guard armed on closed port 127.0.0.1:1: connecting fails with OperationalError
    closed_port_url = "postgresql://postgres:postgres@127.0.0.1:1/truealpha"
    monkeypatch.setenv("ALLOW_CLEAR_TEST", "1")
    monkeypatch.setenv("DATABASE_URL", closed_port_url)
    with pytest.raises(psycopg.OperationalError):
        with psycopg.connect(closed_port_url, connect_timeout=1):
            pass


def test_guard_fails_loudly_when_require_runtime_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """When TRUEALPHA_REQUIRE_RUNTIME is set, skip_or_fail turns skips into hard failures."""
    monkeypatch.setenv("TRUEALPHA_REQUIRE_RUNTIME", "1")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("ALLOW_CLEAR_TEST", raising=False)

    with pytest.warns(UserWarning, match="protect ambient databases"):
        with pytest.raises(pytest.fail.Exception, match="TRUEALPHA_REQUIRE_RUNTIME is set"):
            guard_db_write()
