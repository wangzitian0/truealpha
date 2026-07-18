"""#396: runs db/tests/conversations_contract.sql against a live Postgres.

Mirrors apps/data-engine/tests/datahub/test_evidence_graph_repository.py's
`test_db_contract_executes` pattern: the contract SQL is the source of
truth for RLS/append-only/single-redemption behavior; this test exists so
it actually executes in CI rather than sitting unreferenced (unlike
db/tests/governed_research_access_contract.sql, which nothing currently
runs — see the PR discussion on #396).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import psycopg
import pytest

_CONTRACT_SQL = Path(__file__).resolve().parents[3] / "db" / "tests" / "conversations_contract.sql"
_DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/truealpha"


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)


@pytest.fixture
def database_url() -> str:
    url = _database_url()
    try:
        conn = psycopg.connect(url, connect_timeout=3)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    else:
        conn.close()
    return url


def test_conversations_db_contract_executes(database_url: str) -> None:
    assert _CONTRACT_SQL.exists(), f"missing contract SQL at {_CONTRACT_SQL}"
    completed = subprocess.run(
        ["psql", database_url, "-v", "ON_ERROR_STOP=1", "-f", str(_CONTRACT_SQL)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
