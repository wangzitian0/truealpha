"""`MartResearchReadRepository` against a real local Postgres (skip gracefully without
one) — see #369 (reopened by the #429 drift audit: the closing PR shipped only the
fixture-backed reader, so `build_research_report` had no deployed, real-data consumer).

Mirrors test_strategy_run_postgres.py's pattern: real, obviously-fake rows committed
under a unique strategy_key (not rolled back, since PostgresStrategyRunRepository opens
its own connection per call and would not see uncommitted rows from this test's own
transaction).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from truealpha_contracts.execution import AvailabilityStatus
from truealpha_contracts.research import ValuationTier
from truealpha_runtime.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_runtime.research_report import ResearchReportKind, ResearchReportRequest, build_research_report
from truealpha_runtime.research_report_mart import MartResearchReadRepository
from truealpha_runtime.testing import guard_db_write, open_test_connection

pytestmark = pytest.mark.deps

_HASH64 = "d" * 64
_CUTOFF = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)


def _resolve_database_url() -> str:
    return guard_db_write()


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
    with open_test_connection(autocommit=True) as active:
        yield active


def _govern_run(connection, strategy_run_id: str) -> None:
    tok = uuid.uuid4().hex + "0" * 32
    policy_id = f"mart-test-policy:{tok[:12]}"
    sp_id = f"schedule-policy:{tok}"
    scope_id = f"capture-scope:{tok}"
    env_row = connection.execute("select environment from mart.environment_identity").fetchone()
    env_name = str(env_row[0]) if env_row else "production"
    cutoff = "2026-06-30T00:00:00Z"
    camp_row = connection.execute(
        "with p as (select jsonb_build_object('campaign_policy_id', %s::text, 'environment', %s::text, 'cutoff', %s::text, "
        "'universe_refs', jsonb_build_array(jsonb_build_object('content_sha256', repeat('d', 64), 'universe_id', 'universe:topt-us-2026-03-31', 'universe_version', '2026-03-31'))) as payload) "
        "select 'capture-campaign:' || raw.canonical_sha256(jsonb_build_object('kind', 'capture-campaign', 'identity', payload)), "
        "raw.canonical_sha256(payload), (payload->'universe_refs')::text from p",
        (policy_id, env_name, cutoff),
    ).fetchone()
    camp_id, camp_sha, urefs_json = str(camp_row[0]), str(camp_row[1]), str(camp_row[2])
    connection.execute(
        "insert into raw.capture_campaigns (campaign_id, content_sha256, policy_id, environment, cutoff, cutoff_canonical, universe_refs) "
        "values (%s, %s, %s, %s, %s::timestamptz, %s, %s::jsonb)",
        (camp_id, camp_sha, policy_id, env_name, cutoff, cutoff, urefs_json),
    )
    connection.execute(
        "insert into raw.capture_schedule_policies (schedule_policy_id, content_sha256, policy_version, demanded_cadence, provider_availability_cadence, freshness_max_age, retry_policy, payload) "
        "values (%s, %s, %s, interval '1 day', 'manual-only:v1', interval '30 days', '{}'::jsonb, '{}'::jsonb)",
        (sp_id, tok, policy_id),
    )
    run_row = connection.execute(
        "with p as (select jsonb_build_object('campaign_id', %s::text, 'run_sequence', 1, 'schedule_policy_id', %s::text, 'capture_scope_id', %s::text) as payload) "
        "select 'capture-run:' || raw.canonical_sha256(jsonb_build_object('kind', 'capture-run', 'identity', payload)), "
        "raw.canonical_sha256(payload) from p",
        (camp_id, sp_id, scope_id),
    ).fetchone()
    cap_id, cap_sha = str(run_row[0]), str(run_row[1])
    connection.execute(
        "insert into raw.capture_runs (run_id, campaign_id, run_sequence, schedule_policy_id, capture_scope_id, content_sha256) "
        "values (%s, %s, 1, %s, %s, %s)",
        (cap_id, camp_id, sp_id, scope_id, cap_sha),
    )
    connection.execute(
        "insert into staging.evidence_nodes (node_id, kind, content_sha256, valid_from, transaction_time, recorded_at) "
        "values (%s, 'capture_run', %s, '2026-03-31', now(), now())",
        (cap_id, cap_id.split(":", 1)[1]),
    )
    connection.execute(
        "insert into mart.strategy_run_capture_bindings (capture_run_id, strategy_run_id, bound_by) "
        "values (%s, %s, 'tick')",
        (cap_id, strategy_run_id),
    )
    seq_row = connection.execute(
        "select coalesce(max(sequence), -1) + 1 from mart.current_pointer "
        "where environment = %s "
        "and universe_id = 'universe:topt-us-2026-03-31' and universe_version = '2026-03-31' and factor_id = 'gross_profit_per_employee'",
        (env_name,),
    ).fetchone()
    next_seq = int(seq_row[0]) if seq_row else 0
    prev_row = connection.execute(
        "select target_run_id from mart.current_pointer_head "
        "where environment = %s "
        "and universe_id = 'universe:topt-us-2026-03-31' and universe_version = '2026-03-31' and factor_id = 'gross_profit_per_employee'",
        (env_name,),
    ).fetchone()
    prev_run_id = str(prev_row[0]) if prev_row and next_seq > 0 else None
    connection.execute(
        "insert into mart.current_pointer (pointer_id, content_sha256, environment, universe_id, universe_version, "
        "factor_id, target_run_id, sequence, previous_run_id, advanced_at) "
        "values (%s, %s, %s, 'universe:topt-us-2026-03-31', "
        "'2026-03-31', 'gross_profit_per_employee', %s, %s, %s, now())",
        (f"current-pointer:{tok}", tok, env_name, cap_id, next_seq, prev_run_id),
    )


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
    _govern_run(connection, run_id)


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

    # #1624: THEME_RANKING when no decisions match the requested cutoff must also return a
    # missing subject rather than an empty tuple that fails ResearchReport.assemble(min_length=1).
    theme_req = ResearchReportRequest(
        report_kind=ResearchReportKind.THEME_RANKING,
        target_entity_ids=("theme:qqq",),
        cutoff_at=datetime(2099, 12, 31, 23, 59, 59, tzinfo=UTC),
        strategy_id="large_model_value_v0",
    )
    theme_report = build_research_report(theme_req, repository, context=_context())
    assert len(theme_report.subjects) == 1
    assert theme_report.subjects[0].sections[0].availability is AvailabilityStatus.UNAVAILABLE
    assert theme_report.subjects[0].sections[0].reason_codes == ("strategy_run_unavailable",)


def test_requires_database_url_or_strategy_repository() -> None:
    with pytest.raises(TypeError):
        MartResearchReadRepository()


def test_panel_head_resolution_has_no_fallback_and_node_latest_uses_mart_readonly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from truealpha_runtime import research_report_mart

    assert not hasattr(research_report_mart, "ACCEPTANCE_FALLBACK_HEAD_SQL")

    executed: list[tuple[str, object]] = []

    class _DummyConn:
        def __enter__(self) -> _DummyConn:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def execute(self, sql: str, params: object = None) -> _DummyConn:
            executed.append((sql, params))
            return self

        def fetchone(self) -> None:
            return None

        def fetchall(self) -> list[object]:
            return []

    monkeypatch.setattr(research_report_mart.psycopg, "connect", lambda *_a, **_k: _DummyConn())
    out = research_report_mart.PostgresMetricNodeValueRepository(database_url="postgresql://unused").latest(
        node_key="gppe_uniform_charge_v0"
    )
    assert isinstance(out, research_report_mart.MetricNodeValueUnavailable)
    assert out.reason == "no served head for strategy"
    sqls = [sql for sql, _ in executed]
    assert sqls[0] == "set local role mart_readonly"
    assert sqls[1] == "select set_config('statement_timeout', '5s', true)"
    assert sqls[-1] == "reset role"
    assert all("capture_status" not in s and "datahub_quality_report" not in s for s in sqls)


def test_the_peg_section_is_available_when_an_excluded_decision_carries_a_peg() -> None:
    """#1804: PEG has its own availability. A decision status applies only when PEG is absent."""
    from truealpha_contracts.execution import AvailabilityStatus
    from truealpha_runtime.research_report_mart import _peg_availability

    assert _peg_availability(True, AvailabilityStatus.EXCLUDED) is AvailabilityStatus.AVAILABLE
    assert _peg_availability(True, AvailabilityStatus.NOT_APPLICABLE) is AvailabilityStatus.AVAILABLE
    assert _peg_availability(False, AvailabilityStatus.AVAILABLE) is AvailabilityStatus.UNAVAILABLE
    assert _peg_availability(False, AvailabilityStatus.EXCLUDED) is AvailabilityStatus.EXCLUDED
