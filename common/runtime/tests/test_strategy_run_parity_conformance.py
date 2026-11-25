"""#469: twin-parity conformance, Python half (against a real local Postgres;
skips gracefully without one, fails hard when TRUEALPHA_REQUIRE_RUNTIME is set).

Seeds the shared cases from ``conformance/strategy_run_parity.json`` and asserts
the shipping Python consumer's serialized output equals the frozen canon that
the TS twin (``apps/app-web/tests/strategy-run-parity-conformance.test.ts``)
must also produce — two languages, one schema, one expected byte shape. The
divergence-trigger cases (a run recorded under a non-default key, ``rank = 0``,
``target_weight = 1.5``) must come back ``schema_mismatch`` on BOTH sides.

#1235: the canon holds two report cases under the one strategy key. ``report`` is a run of
the reviewed definition 1.0.0; ``report_definition_not_in_registry`` is a run of the deleted
0.1.0, whose review state is ``unknown``. Each test seeds one report case only, so the
repository's "latest run" is the case under test.

Each test runs inside ONE transaction: the repository under test opens its own
connection per call, so ``psycopg.connect`` is redirected onto the test's
connection (the same seam the TS half injects through ``db.ts __setTestClient``,
and the same technique as test_materialization's ``_BorrowedConnection``).
Nothing is committed — the fixture's fixed far-future ``executed_at`` can never
poison other tests' "latest run" isolation on a shared database.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from truealpha_runtime.access import AccessContext, AuthenticationMethod, PrincipalKind
from truealpha_runtime.strategy_run import StrategyRunReport, StrategyRunUnavailable
from truealpha_runtime.strategy_run_postgres import PostgresStrategyRunRepository

pytestmark = pytest.mark.deps

_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "contracts" / "tests" / "fixtures" / "strategy_run_parity.json"
_FIXTURE = json.loads(_FIXTURE_PATH.read_text())
_REPORT_CASES = sorted(name for name, case in _FIXTURE.items() if "expected_report" in case)
_DIVERGENCE_CASES = ("wrong_strategy_id", "rank_zero", "weight_out_of_bounds")

_RUN_SQL = """insert into mart.strategy_runs
 (strategy_run_id, content_sha256, strategy_key, strategy_version, definition_content_sha256,
  corpus_sha256, claim_ceiling, executed_at)
 values (%(strategy_run_id)s, %(content_sha256)s, %(strategy_key)s, %(strategy_version)s,
  %(definition_content_sha256)s, %(corpus_sha256)s, %(claim_ceiling)s, %(executed_at)s)"""

_DECISION_SQL = """insert into mart.strategy_decisions
 (strategy_decision_id, content_sha256, strategy_run_id, issuer_id, cutoff_at,
  capital_adjusted_labor_efficiency, tier, current_price_to_sales, target_price_to_sales,
  current_price_to_gross_profit, target_price_to_gross_profit,
  valuation_gap, eligible, outcome, exclusion_reason, rank, target_weight, peg, peg_rank)
 values (%(strategy_decision_id)s, %(content_sha256)s, %(strategy_run_id)s, %(issuer_id)s,
  %(cutoff_at)s, %(capital_adjusted_labor_efficiency)s, %(tier)s, %(current_price_to_sales)s,
  %(target_price_to_sales)s, %(current_price_to_gross_profit)s, %(target_price_to_gross_profit)s,
  %(valuation_gap)s, %(eligible)s, %(outcome)s, %(exclusion_reason)s,
  %(rank)s, %(target_weight)s, %(peg)s, %(peg_rank)s)"""


def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL")


class _BorrowedConnection:
    """Redirects the repository's per-call ``psycopg.connect`` onto the test's
    own transaction; close/commit are withheld so the rollback owns the data."""

    def __init__(self, real: psycopg.Connection) -> None:
        self._real = real

    def __enter__(self) -> _BorrowedConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def cursor(self, **kwargs):
        return self._real.cursor(**kwargs)


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch):
    from truealpha_runtime.testing import skip_or_fail

    url = _database_url()
    if not url:
        skip_or_fail("DATABASE_URL must be explicitly provided for DB parity tests (no default to port 5432)")
        return
    try:
        connection = psycopg.connect(url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; ci-python runs this against a real database")
    try:
        monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: _BorrowedConnection(connection))
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _seed(connection: psycopg.Connection, case: dict, *, capture_run_id: str | None = None) -> str:
    import uuid

    strategy_run_id = case["run"]["strategy_run_id"]
    connection.execute(_RUN_SQL, case["run"])
    for decision in case["decisions"]:
        connection.execute(_DECISION_SQL, {**decision, "strategy_run_id": strategy_run_id})
    cap_id = capture_run_id
    if cap_id is None:
        tok = uuid.uuid4().hex + "0" * 32
        policy_id = f"parity-policy:{tok[:12]}"
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
        assert camp_row is not None
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
        assert run_row is not None
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
    connection.execute(
        "insert into mart.strategy_run_capture_bindings (capture_run_id, strategy_run_id, bound_by) "
        "values (%s, %s, 'tick')",
        (cap_id, strategy_run_id),
    )
    return cap_id


def _context() -> AccessContext:
    now = datetime(2026, 7, 23, tzinfo=UTC)
    return AccessContext(
        context_id="ctx:parity",
        principal_id="principal:parity",
        tenant_id="tenant:parity",
        session_id="session:parity",
        authentication_method=AuthenticationMethod.SERVICE_IDENTITY,
        principal_kind=PrincipalKind.SERVICE,
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )


def test_the_canon_holds_a_reviewed_and_an_unknown_report_case() -> None:
    """#1235: both twins must agree on each review state that real runs carry today."""
    states = sorted(_FIXTURE[name]["expected_report"]["definition"]["review_state"] for name in _REPORT_CASES)
    assert states == ["reviewed", "unknown"], f"the canon's report cases carry review states {states}"


@pytest.mark.parametrize("case_name", _REPORT_CASES)
def test_report_serialization_matches_the_frozen_canon(database, case_name: str) -> None:
    case = _FIXTURE[case_name]
    _seed(database, case)
    repository = PostgresStrategyRunRepository(database_url=_database_url())
    result = repository.get_latest(strategy_id=case["request_strategy_id"], context=_context())
    assert isinstance(result, StrategyRunReport), f"expected a report, got {result!r}"
    dumped = result.model_dump(mode="json")
    assert dumped.pop("head_freshness") == "fresh"
    assert isinstance(dumped.pop("head_age_hours"), float)
    dumped.pop("head_limit_hours", None)
    assert dumped == case["expected_report"]


def test_divergence_triggers_are_schema_mismatch(database) -> None:
    shared_cap: str | None = None
    for name in _DIVERGENCE_CASES:
        shared_cap = _seed(database, _FIXTURE[name], capture_run_id=shared_cap)
    repository = PostgresStrategyRunRepository(database_url=_database_url())
    for name in _DIVERGENCE_CASES:
        case = _FIXTURE[name]
        result = repository.get_latest(strategy_id=case["request_strategy_id"], context=_context())
        assert isinstance(result, StrategyRunUnavailable), f"{name}: expected unavailable, got {result!r}"
        assert result.reason == case["expected_reason"], f"{name}: {result.reason}"
