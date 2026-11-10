# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import os; from unittest.mock import patch; import psycopg; import pytest; from truealpha_runtime.testing import ( GATE_MARKER, can_write_test_database, db_writes_allowed, guard_db_write, isolated_test_database, )
@pytest.fixture(autouse=True)
def _isolate_guard_env(monkeypatch: pytest.MonkeyPatch) -> None: monkeypatch.delenv("TRUEALPHA_REQUIRE_RUNTIME", raising=False)
def test_guard_skips_when_write_not_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False); monkeypatch.delenv("ALLOW_CLEAR_TEST", raising=False); monkeypatch.delenv("DATABASE_URL", raising=False); assert not can_write_test_database(); assert not db_writes_allowed()
    with patch("psycopg.connect") as mock_connect:
        with pytest.warns(UserWarning, match="protect ambient databases"):
            with pytest.raises(pytest.skip.Exception, match="protect ambient databases"): guard_db_write()
        mock_connect.assert_not_called()
def test_guard_rejects_unset_database_url_when_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_CLEAR_TEST", "1"); monkeypatch.delenv("DATABASE_URL", raising=False); assert can_write_test_database(); assert not db_writes_allowed()
    with patch("psycopg.connect") as mock_connect:
        with pytest.warns(UserWarning, match="no default to port 5432"):
            with pytest.raises(pytest.skip.Exception, match="no default to port 5432"): guard_db_write()
        mock_connect.assert_not_called()
def test_guard_passes_explicit_database_url_when_authorized(monkeypatch: pytest.MonkeyPatch) -> None: monkeypatch.setenv("CI", "true"); monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@127.0.0.1:54329/testdb"); assert db_writes_allowed(); url = guard_db_write(); assert url == "postgresql://user:pass@127.0.0.1:54329/testdb"
def test_root_gate_refuses_unprotected_psycopg_connect_and_fails_when_removed( monkeypatch: pytest.MonkeyPatch, ) -> None:
    assert getattr(psycopg.connect, GATE_MARKER, False) is True, ( "root conftest.py must install the psycopg.connect write gate at import time" ); real_connect = getattr(psycopg.connect, "_real_connect"); assert callable(real_connect); monkeypatch.delenv("CI", raising=False); monkeypatch.delenv("ALLOW_CLEAR_TEST", raising=False); closed_port_url = "postgresql://postgres:x@127.0.0.1:1/probe"; monkeypatch.setenv("DATABASE_URL", closed_port_url)
    with pytest.warns(UserWarning, match="DB-backed test skipped: set CI=true or ALLOW_CLEAR_TEST=1"):
        with pytest.raises( pytest.skip.Exception, match="DB-backed test skipped: set CI=true or ALLOW_CLEAR_TEST=1 and DATABASE_URL to allow database access", ): psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=1)
    monkeypatch.setattr(psycopg, "connect", real_connect)
    with pytest.raises(psycopg.OperationalError): psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=1)
def test_mutation_check_unprotected_write_fails_against_closed_port( monkeypatch: pytest.MonkeyPatch, ) -> None:
    monkeypatch.delenv("CI", raising=False); monkeypatch.delenv("ALLOW_CLEAR_TEST", raising=False); monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("psycopg.connect") as mock_connect:
        with pytest.warns(UserWarning, match="protect ambient databases"):
            with pytest.raises(pytest.skip.Exception):
                with isolated_test_database("mutation_check"): pass
        mock_connect.assert_not_called()
    closed_port_url = "postgresql://postgres:postgres@127.0.0.1:1/truealpha"; monkeypatch.setenv("ALLOW_CLEAR_TEST", "1"); monkeypatch.setenv("DATABASE_URL", closed_port_url)
    with pytest.raises(psycopg.OperationalError):
        with psycopg.connect(closed_port_url, connect_timeout=1): pass
def test_guard_fails_loudly_when_require_runtime_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUEALPHA_REQUIRE_RUNTIME", "1"); monkeypatch.delenv("CI", raising=False); monkeypatch.delenv("ALLOW_CLEAR_TEST", raising=False)
    with pytest.warns(UserWarning, match="protect ambient databases"):
        with pytest.raises(pytest.fail.Exception, match="TRUEALPHA_REQUIRE_RUNTIME is set"): guard_db_write()
