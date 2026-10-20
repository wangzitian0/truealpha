"""Module 3 and module 4 reads against a real Postgres (#42).

The seed comes from `conformance/issuer_modules_parity.json`, the canon the App twin also
reads. One served run holds the expected rows; one unserved run holds a newer, available
decoy. A read that drops the `mart.served_head` pin returns the decoy and fails here.

Each test runs inside one transaction that is rolled back: the readers open their own
connection per call, so `psycopg.connect` is redirected onto the test's connection (the
technique of test_strategy_run_parity_conformance.py). `session_replication_role = replica`
lets the pointer name a capture run without the evidence-graph rows behind it.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus
from truealpha_contracts.issuer_modules_mart import PostgresIssuerModuleReader
from truealpha_contracts.research_report import (
    ReportSectionKind,
    ResearchReportKind,
    ResearchReportRequest,
    build_research_report,
)
from truealpha_contracts.research_report_mart import MartResearchReadRepository

_SEED_PATH = Path(__file__).parents[1] / "conformance" / "issuer_modules_parity.py"
_SPEC = importlib.util.spec_from_file_location("issuer_modules_parity_seed", _SEED_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_SEED = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SEED)
CANON = _SEED.CANON
_HASH64 = "e" * 64
_CUTOFF = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)


class _BorrowedConnection:
    """The reader's per-call connection, redirected onto the test's transaction."""

    def __init__(self, real: psycopg.Connection) -> None:
        self._real = real

    def __enter__(self) -> _BorrowedConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def cursor(self, **kwargs: Any) -> psycopg.Cursor[Any]:
        return self._real.cursor(**kwargs)


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch):
    from truealpha_runtime.testing import skip_or_fail

    url = os.environ.get("DATABASE_URL")
    if not url:
        skip_or_fail("DATABASE_URL must be explicitly provided for the module read tests")
        return
    try:
        connection = psycopg.connect(url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
    try:
        connection.execute("set local session_replication_role = replica")
        monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: _BorrowedConnection(connection))
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _seed_module_rows(connection: psycopg.Connection) -> None:
    _SEED.seed_module_rows(connection)


def _seed_served_head(connection: psycopg.Connection) -> None:
    _SEED.seed_served_head(connection)


def test_the_reader_returns_the_served_row_and_not_the_newer_decoy(database: psycopg.Connection) -> None:
    _seed_module_rows(database)
    _seed_served_head(database)
    reader = PostgresIssuerModuleReader(database_url="postgresql://borrowed")

    supply_chain = reader.supply_chain_exposure(CANON["issuer_id"])
    analyst = reader.analyst_ratings(CANON["issuer_id"])

    assert supply_chain is not None and analyst is not None
    assert supply_chain.model_dump(mode="json") == CANON["supply_chain_exposure"]["expected"]
    assert analyst.model_dump(mode="json") == CANON["analyst_ratings"]["expected"]


def test_no_served_run_means_no_row(database: psycopg.Connection) -> None:
    """Rows exist, but no head serves their run: the read answers None, never a bare latest."""
    _seed_module_rows(database)
    reader = PostgresIssuerModuleReader(database_url="postgresql://borrowed")

    assert reader.supply_chain_exposure(CANON["issuer_id"]) is None
    assert reader.analyst_ratings(CANON["issuer_id"]) is None


def _seed_strategy_decision(connection: psycopg.Connection, *, peg: str, peg_rank: int) -> None:
    run_id = "strategy-run:" + uuid.uuid4().hex + "0" * 32
    connection.execute(
        """insert into mart.strategy_runs
           (strategy_run_id, content_sha256, strategy_key, strategy_version, definition_content_sha256,
            corpus_sha256, claim_ceiling, executed_at)
           values (%s, %s, 'large_model_value_v0', 'v0', %s, %s, 'preview', '2999-12-31T00:00:00Z')""",
        (run_id, _HASH64, _HASH64, _HASH64),
    )
    connection.execute(
        """insert into mart.strategy_decisions
           (strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
            capital_adjusted_labor_efficiency, tier, current_price_to_sales, target_price_to_sales,
            valuation_gap, eligible, outcome, exclusion_reason, rank, target_weight, peg, peg_rank)
           values (%s, %s, %s, %s, %s, '272539.18', 'large_model_native', '12.5', '18.75', '0.5',
                   true, 'selected', null, 1, '1.0', %s, %s)""",
        (
            "strategy-decision:" + uuid.uuid4().hex + "0" * 32,
            _HASH64,
            run_id,
            CANON["issuer_id"],
            _CUTOFF,
            peg,
            peg_rank,
        ),
    )


def _context() -> AccessContext:
    now = datetime.now(UTC)
    return AccessContext(
        context_id="ctx:test",
        principal_id="principal:test",
        tenant_id="tenant:test",
        session_id="session:test",
        authentication_method=AuthenticationMethod.SERVICE_IDENTITY,
        principal_kind=PrincipalKind.SERVICE,
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )


def test_the_deployed_reader_fills_peg_supply_chain_and_analyst_sections_from_mart(
    database: psycopg.Connection,
) -> None:
    """#42: through `MartResearchReadRepository(database_url=...)`, the construction the MCP
    server uses. Before #42 these three sections always read "not materialized"."""
    _seed_module_rows(database)
    _seed_served_head(database)
    _seed_strategy_decision(database, peg="1.2300", peg_rank=4)

    report = build_research_report(
        ResearchReportRequest(
            report_kind=ResearchReportKind.COMPANY, target_entity_ids=(CANON["issuer_id"],), cutoff_at=_CUTOFF
        ),
        MartResearchReadRepository(database_url="postgresql://borrowed"),
        context=_context(),
    )
    sections = {section.section_kind: section for section in report.subjects[0].sections}

    peg = sections[ReportSectionKind.PEG_CONVENTIONS]
    assert peg.availability is AvailabilityStatus.AVAILABLE
    assert {result.label: result.value for result in peg.results} == {"peg": "1.2300", "peg_rank": "4"}

    supply_chain = sections[ReportSectionKind.SUPPLY_CHAIN]
    expected = CANON["supply_chain_exposure"]["expected"]
    assert supply_chain.availability is AvailabilityStatus.AVAILABLE
    assert supply_chain.validation_status is FactorValidationStatus.NOT_EVALUATED
    assert supply_chain.reason_codes == tuple(expected["reason_codes"])
    assert {result.label: result.value for result in supply_chain.results}["exposure_score"] == "0.4200"
    assert all(result.trace is not None and CANON["served_run_id"] in result.trace.reference_id
               for result in supply_chain.results)  # fmt: skip

    analyst = sections[ReportSectionKind.ANALYST_HISTORY]
    assert analyst.availability is AvailabilityStatus.AVAILABLE
    assert {result.label: result.value for result in analyst.results}["consensus_rating"] == "4.3500"
    assert all(str(result.cutoff_at.date()) == "2026-06-30" for result in analyst.results)
