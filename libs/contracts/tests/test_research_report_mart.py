"""`MartResearchReadRepository` against a real local Postgres (skip gracefully without
one) — see #369 (reopened by the #429 drift audit: the closing PR shipped only the
fixture-backed reader, so `build_research_report` had no deployed, real-data consumer).

Mirrors test_strategy_run_postgres.py's pattern: real, obviously-fake rows committed
under a unique strategy_key (not rolled back, since PostgresStrategyRunRepository opens
its own connection per call and would not see uncommitted rows from this test's own
transaction).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from truealpha_contracts.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_contracts.execution import AvailabilityStatus
from truealpha_contracts.research import ValuationTier
from truealpha_contracts.research_report import ResearchReportKind, ResearchReportRequest, build_research_report
from truealpha_contracts.research_report_mart import MartResearchReadRepository

_DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/truealpha"
_HASH64 = "d" * 64
_CUTOFF = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)


def _resolve_database_url() -> str:
    return os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)


def _unique_strategy_key() -> str:
    return f"test-research-report-mart-{uuid.uuid4().hex}"


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


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(_resolve_database_url(), connect_timeout=3, autocommit=True)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.close()


def _insert_run(connection, run_id: str, strategy_key: str) -> None:
    connection.execute(
        """
        insert into mart.strategy_runs (
            strategy_run_id, content_sha256, strategy_key, strategy_version,
            definition_content_sha256, corpus_sha256, claim_ceiling, executed_at
        ) values (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (run_id, _HASH64, strategy_key, "v0", _HASH64, _HASH64, "preview", datetime.now(UTC)),
    )


def _insert_decision(connection, decision_id: str, run_id: str, *, issuer_id: str, rank: int | None) -> None:
    connection.execute(
        """
        insert into mart.strategy_decisions (
            strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
            capital_adjusted_labor_efficiency, tier, current_price_to_sales, target_price_to_sales,
            valuation_gap, eligible, outcome, exclusion_reason, rank, target_weight
        ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            decision_id,
            _HASH64,
            run_id,
            issuer_id,
            _CUTOFF,
            "272539.18",
            ValuationTier.LARGE_MODEL_NATIVE.value,
            "12.5",
            "18.75",
            "0.5",
            True,
            "selected",
            None,
            rank,
            "1.0",
        ),
    )


def test_company_report_reads_real_mart_decisions(connection) -> None:
    # StrategyRunReport.strategy_id is a narrow Literal["large_model_value_v0"] (the only
    # strategy the DTO admits today; see test_strategy_run_postgres.py's identical note) —
    # a disposable per-test key would fail Pydantic validation on read-back
    # (PostgresStrategyRunRepository constructs the report with whatever strategy_id was
    # requested) and surface as schema_mismatch, not a missing/unavailable run. Isolation
    # instead comes from executed_at=now() deterministically outranking any prior run.
    strategy_key = "large_model_value_v0"
    run_id = "strategy-run:" + uuid.uuid4().hex + "0" * 32
    _insert_run(connection, run_id, strategy_key)
    _insert_decision(
        connection, "strategy-decision:" + uuid.uuid4().hex + "0" * 32, run_id, issuer_id="issuer:mart-adm", rank=1
    )

    repository = MartResearchReadRepository(database_url=_resolve_database_url())
    request = ResearchReportRequest(
        report_kind=ResearchReportKind.COMPANY,
        target_entity_ids=("issuer:mart-adm",),
        cutoff_at=_CUTOFF,
        strategy_id=strategy_key,
        title="mart-backed company report",
    )
    report = build_research_report(request, repository, context=_context())

    assert report.generated_from == "mart:research_report.v1"
    assert len(report.subjects) == 1
    subject = report.subjects[0]
    assert subject.subject_id == "issuer:mart-adm"
    efficiency_section = next(s for s in subject.sections if s.section_kind.value == "operating_efficiency")
    assert efficiency_section.availability is AvailabilityStatus.AVAILABLE
    efficiency_result = efficiency_section.results[0]
    assert efficiency_result.value == "272539.18"
    # #369: the trace must say where the data actually came from, not a hardcoded
    # "strategy_smoke_fixture:" literal (the exact bug class fixed in research-read.ts's
    # traceId() for #370) — a mart-backed report's trace must say "mart:...".
    assert efficiency_result.trace is not None
    assert efficiency_result.trace.reference_id.startswith("mart:")
    valuation_section = next(s for s in subject.sections if s.section_kind.value == "valuation")
    assert valuation_section.results[0].value == ValuationTier.LARGE_MODEL_NATIVE.value


def test_ranking_report_reads_real_mart_decisions_in_rank_order(connection) -> None:
    # Same Literal constraint as test_company_report_reads_real_mart_decisions above.
    strategy_key = "large_model_value_v0"
    run_id = "strategy-run:" + uuid.uuid4().hex + "0" * 32
    _insert_run(connection, run_id, strategy_key)
    _insert_decision(
        connection, "strategy-decision:" + uuid.uuid4().hex + "0" * 32, run_id, issuer_id="issuer:mart-second", rank=2
    )
    _insert_decision(
        connection, "strategy-decision:" + uuid.uuid4().hex + "0" * 32, run_id, issuer_id="issuer:mart-first", rank=1
    )

    repository = MartResearchReadRepository(database_url=_resolve_database_url())
    request = ResearchReportRequest(
        report_kind=ResearchReportKind.THEME_RANKING,
        target_entity_ids=("theme:test",),
        cutoff_at=_CUTOFF,
        strategy_id=strategy_key,
        title="mart-backed ranking report",
    )
    report = build_research_report(request, repository, context=_context())

    assert [s.subject_id for s in report.subjects] == ["issuer:mart-first", "issuer:mart-second"]
    assert [s.rank for s in report.subjects] == [1, 2]


def test_missing_subject_when_strategy_key_has_no_runs(connection) -> None:
    repository = MartResearchReadRepository(database_url=_resolve_database_url())
    request = ResearchReportRequest(
        report_kind=ResearchReportKind.COMPANY,
        target_entity_ids=("issuer:does-not-exist",),
        cutoff_at=_CUTOFF,
        strategy_id=_unique_strategy_key(),
    )
    report = build_research_report(request, repository, context=_context())

    assert len(report.subjects) == 1
    section = report.subjects[0].sections[0]
    assert section.availability is AvailabilityStatus.UNAVAILABLE
    assert section.reason_codes == ("strategy_run_unavailable",)


def test_requires_database_url_or_strategy_repository() -> None:
    with pytest.raises(TypeError):
        MartResearchReadRepository()
