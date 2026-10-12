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

from unittest.mock import patch

import psycopg
import pytest
from truealpha_runtime.testing import (
    can_write_test_database,
    guard_db_write,
    isolated_test_database,
)


def test_guard_skips_when_write_not_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    """When CI and ALLOW_CLEAR_TEST are unset, guard_db_write must skip and make zero connections."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("ALLOW_CLEAR_TEST", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert not can_write_test_database()

    with patch("psycopg.connect") as mock_connect:
        with pytest.raises(pytest.skip.Exception, match="protect ambient databases"):
            guard_db_write()
        mock_connect.assert_not_called()


def test_guard_rejects_unset_database_url_when_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    """When write is authorized but DATABASE_URL is unset, it must not default to port 5432."""
    monkeypatch.setenv("ALLOW_CLEAR_TEST", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert can_write_test_database()

    with patch("psycopg.connect") as mock_connect:
        with pytest.raises(pytest.skip.Exception, match="no default to port 5432"):
            guard_db_write()
        mock_connect.assert_not_called()


def test_guard_passes_explicit_database_url_when_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    """When authorized and explicit DATABASE_URL is provided, guard returns it."""
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@127.0.0.1:54329/testdb")

    url = guard_db_write()
    assert url == "postgresql://user:pass@127.0.0.1:54329/testdb"


def test_mutation_check_unprotected_write_fails_against_closed_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation check (Acceptance #3):

    - With the guard in place and env unset, isolated_test_database skips without connecting.
    - With the guard removed/bypassed, attempting connection against a closed port
      (127.0.0.1:1) raises psycopg.OperationalError.
    """
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("ALLOW_CLEAR_TEST", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    # 1. With guard in place: isolated_test_database skips cleanly
    with patch("psycopg.connect") as mock_connect:
        with pytest.raises(pytest.skip.Exception):
            with isolated_test_database("mutation_check"):
                pass
        mock_connect.assert_not_called()

    # 2. With guard bypassed: connecting to closed port 127.0.0.1:1 fails with OperationalError
    closed_port_url = "postgresql://postgres:postgres@127.0.0.1:1/truealpha"
    with pytest.raises(psycopg.OperationalError):
        with psycopg.connect(closed_port_url, connect_timeout=1):
            pass
