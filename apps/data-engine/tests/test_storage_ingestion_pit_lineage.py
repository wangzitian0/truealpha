"""Storage ingestion foundation, PIT lineage, and SSOT contracts (#1258, #1237).

This module tests the four core pillars of Topic 2:
1. Quota SSOT:
   - Every registered vendor source and origin binds a declared ledger seat in LEDGER_CAPACITIES.
   - Rate limit bounds, window durations, and environment shares must be positive and fit within allowances.
   - Deployed source modules route vendor network calls through gateway and api_call_ledger.
2. Scope SSOT & Denominator Integrity:
   - Obligation planning strictly computes the exact Cartesian product of members and semantics.
   - Anti-exclusion: No dropping failing tickers to fabricate a green completion status.
   - mart.topt_capture_status.complete is strictly count(result_id) = count(obligation_id).
3. PIT Storage & §8 Status Dimensions:
   - staging.capture_normalized_observations.knowable_at has NO default clock expression.
   - Constraint recorded_at >= knowable_at prevents physical ingestion before knowable-at cutoff.
   - Retired table staging.financial_facts is absent (to_regclass is null).
   - Three status dimensions (availability, source_evidence, factor_validation) report honest states.
4. 5-Hop Full Lineage Trace:
   - Downstream mart core result -> staging observation -> raw source vintage -> raw fetch -> raw S3 bytes.
   - Lineage integrity trigger raw.validate_capture_source_vintage_lineage() rejects mismatched digests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from data_engine.datahub.control_plane import expand_obligations, replay_retry_policy
from data_engine.datahub.production_topt import source_registrations as registry
from data_engine.datahub.production_topt.source_registrations import (
    LEDGER_CAPACITIES,
    REGISTRATIONS,
    CapacityDeclaration,
    OriginRegistration,
    environment_share,
)
from data_engine.datahub.production_topt.status_dimensions import (
    availability_status_for,
    source_evidence_status_for,
    statuses_without_raw_pointer,
)
from data_engine.datahub.repository import PostgresCaptureControlRepository
from factors.validation_records import validation_status_for
from truealpha_contracts.capture_control import CaptureListVersion
from truealpha_contracts.common import CaptureEnvironment, canonical_sha256
from truealpha_contracts.datahub import CaptureCampaign, CaptureRun, CaptureSchedulePolicy
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus, InputEvidenceStatus
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_ENGINE_DIR = REPO_ROOT / "apps" / "data-engine"
SRC_DIR = DATA_ENGINE_DIR / "src" / "data_engine"
SCRIPTS_DIR = DATA_ENGINE_DIR / "scripts"

_NETWORK_CALL_PATTERN = re.compile(r"urlopen\(|httpx\.Client\(|requests\.(get|post|Session)\(")


# ======================================================================================
# Pillar 1: Quota SSOT
# ======================================================================================


def test_quota_ssot_all_registrations_and_origins_bind_declared_seat() -> None:
    """Every registered source and origin must bind a declared seat in LEDGER_CAPACITIES."""
    for registration in REGISTRATIONS:
        if registration.corroboration_class != "release":
            assert registration.ledger_seat in LEDGER_CAPACITIES, (
                f"{registration.source_id} ledger seat '{registration.ledger_seat}' is undeclared"
            )
            assert registration.capacity is LEDGER_CAPACITIES[registration.ledger_seat]

        for origin in registration.origins:
            assert origin.ledger_seat in LEDGER_CAPACITIES, (
                f"{origin.origin_source} origin seat '{origin.ledger_seat}' is undeclared"
            )
            assert origin.capacity is LEDGER_CAPACITIES[origin.ledger_seat]

    all_seats = {
        origin.ledger_seat: origin.origin_source for registration in REGISTRATIONS for origin in registration.origins
    }
    assert {"yahoo", "twelvedata", "moomoo"} <= set(all_seats)
    assert registry.registration_for("financial-fact").ledger_seat == "sec"


def test_quota_ssot_capacity_constraints_and_environment_shares() -> None:
    """Capacity declarations must enforce positive rate bounds and safe environment shares."""
    for seat_name, declaration in LEDGER_CAPACITIES.items():
        assert declaration.calls_per_window > 0, f"seat {seat_name} calls per window must be positive"
        assert declaration.window_seconds > 0, f"seat {seat_name} window seconds must be positive"
        if declaration.daily_budget is not None:
            assert declaration.daily_budget > 0, f"seat {seat_name} daily budget must be positive"

        # Validate that declared environment shares fit within total capacity
        if declaration.environment_shares:
            shares_dict = dict(declaration.environment_shares)
            assert set(shares_dict.keys()) <= {"production", "staging"}
            assert sum(shares_dict.values()) <= 100

            if declaration.calls_per_window is not None:
                prod_calls = environment_share(
                    declaration.calls_per_window, declaration.environment_shares, "production"
                )
                staging_calls = environment_share(
                    declaration.calls_per_window, declaration.environment_shares, "staging"
                )
                assert prod_calls is not None and prod_calls > 0
                assert staging_calls is not None and staging_calls > 0
                assert prod_calls + staging_calls <= declaration.calls_per_window


def test_quota_ssot_origin_registration_refuses_undeclared_or_conflicting_seat() -> None:
    """OriginRegistration constructor rejects undeclared seats or contradictory capacities."""
    with pytest.raises(ValueError, match="no capacity declaration"):
        OriginRegistration("probe:v1", "origin:probe", "close", ("p:v1",), ledger_seat="undeclared-seat")

    with pytest.raises(ValueError, match="contradicts the 'sec' seat"):
        OriginRegistration("probe:v1", "origin:probe", "close", ("p:v1",), CapacityDeclaration(5, 1), "sec")


def test_quota_ssot_vendor_call_sites_must_import_gateway() -> None:
    """All external vendor network calls must route through sources.gateway and the ledger."""
    offenders: list[str] = []
    # Scan production source tree
    for path in sorted(SRC_DIR.rglob("*.py")):
        if path.name == "gateway.py":
            continue
        text = path.read_text(encoding="utf-8")
        if _NETWORK_CALL_PATTERN.search(text) and "from data_engine.sources import gateway" not in text:
            offenders.append(str(path.relative_to(DATA_ENGINE_DIR)))

    # Scan operational scripts
    for path in sorted(SCRIPTS_DIR.glob("*.py")):
        if path.name in {"capture_factor_matrix_samples.py", "capture_strategy_evidence.py"}:
            # One-off test corpus extraction scripts targeting static fixture assets
            continue
        text = path.read_text(encoding="utf-8")
        if _NETWORK_CALL_PATTERN.search(text) and "from data_engine.sources import gateway" not in text:
            offenders.append(str(path.relative_to(DATA_ENGINE_DIR)))

    assert not offenders, f"Network call sites found bypassing external call ledger: {offenders}"


# ======================================================================================
# Pillar 2: Scope SSOT & Denominator Integrity
# ======================================================================================


def test_scope_ssot_expand_obligations_strictly_preserves_denominator() -> None:
    """Obligations denominator equals exactly len(members) * len(semantic_types)."""
    universe = UniverseRef(
        universe_id="universe:scope-ssot-test",
        universe_version="v1",
        content_sha256=canonical_sha256({"scope": "test"}),
    )
    members = (
        SubjectRef(kind=SubjectKind.LISTING, id="listing:xnys:aapl"),
        SubjectRef(kind=SubjectKind.LISTING, id="listing:xnys:msft"),
        SubjectRef(kind=SubjectKind.LISTING, id="listing:xnys:nvda"),
    )
    list_version = CaptureListVersion(universe=universe, members=members, effective_at=datetime.now(UTC))
    semantics = ("market-price", "financial-fact")

    obligations = expand_obligations(
        run_id="capture-run:" + "1" * 64,
        list_version=list_version,
        semantic_types=semantics,
        partition="2026-04-01",
    )

    # Denominator must equal len(members) * len(semantics)
    assert len(obligations) == len(members) * len(semantics)
    assert len(obligations) == 6

    # Order must be deterministic: members preserved, semantics sorted
    expected_pairs = [(member.id, semantic) for member in members for semantic in sorted(semantics)]
    observed_pairs = [
        (ob.obligation.subject.id, ob.obligation.capture_requirement_id.removesuffix(":v1")) for ob in obligations
    ]
    assert observed_pairs == expected_pairs


def test_scope_ssot_expand_obligations_refuses_invalid_scope() -> None:
    """Obligations planning rejects empty or duplicate semantics and invalid subject kinds."""
    universe = UniverseRef(
        universe_id="universe:scope-ssot-err",
        universe_version="v1",
        content_sha256=canonical_sha256({"scope": "err"}),
    )
    list_version = CaptureListVersion(
        universe=universe,
        members=(SubjectRef(kind=SubjectKind.LISTING, id="listing:xnys:aapl"),),
        effective_at=datetime.now(UTC),
    )

    with pytest.raises(ValueError, match="semantic_types must not be empty"):
        expand_obligations(
            run_id="capture-run:" + "1" * 64,
            list_version=list_version,
            semantic_types=(),
            partition="2026-04-01",
        )

    with pytest.raises(ValueError, match="duplicates"):
        expand_obligations(
            run_id="capture-run:" + "1" * 64,
            list_version=list_version,
            semantic_types=("market-price", "market-price"),
            partition="2026-04-01",
        )

    invalid_members_list = CaptureListVersion(
        universe=universe,
        members=(SubjectRef(kind=SubjectKind.ISSUER, id="issuer:cik:123"),),
        effective_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="requires listing or security members"):
        expand_obligations(
            run_id="capture-run:" + "1" * 64,
            list_version=invalid_members_list,
            semantic_types=("market-price",),
            partition="2026-04-01",
        )


# ======================================================================================
# Pillar 3: PIT Storage & §8 Status Dimensions
# ======================================================================================


def test_status_dimensions_availability_prioritizes_unavailable_over_stale() -> None:
    """Unavailable figure beats stale; low confidence maps to low_confidence."""
    # Missing figure has no age
    status = availability_status_for(
        availability="unavailable",
        freshness="stale",
        confidence=Decimal("0.95"),
        reason_codes=("missing_metric",),
    )
    assert status is AvailabilityStatus.UNAVAILABLE

    # Available and fresh
    assert (
        availability_status_for(
            availability="available",
            freshness="fresh",
            confidence=Decimal("0.85"),
            reason_codes=(),
        )
        is AvailabilityStatus.AVAILABLE
    )

    # Below confidence floor 0.50
    assert (
        availability_status_for(
            availability="available",
            freshness="fresh",
            confidence=Decimal("0.49"),
            reason_codes=(),
        )
        is AvailabilityStatus.LOW_CONFIDENCE
    )


def test_status_dimensions_source_evidence_requires_dereferenced_pointers() -> None:
    """Missing or unresolved raw pointers must return degraded or rejected, never verified."""
    assert statuses_without_raw_pointer() == (
        InputEvidenceStatus.DEGRADED,
        FactorValidationStatus.NOT_EVALUATED,
    )

    class _MockConn:
        def __init__(self, mapping: dict[str, int | None]) -> None:
            self.mapping = mapping

        def execute(self, sql: str, params: Any) -> Any:
            (ids,) = params

            class _Result:
                def __init__(inner, rows: list[tuple[str, int | None]]) -> None:
                    inner.rows = rows

                def fetchall(inner) -> list[tuple[str, int | None]]:
                    return inner.rows

            return _Result([(oid, self.mapping[oid]) for oid in ids if oid in self.mapping])

    # Unresolved pointer returns REJECTED
    conn_broken = _MockConn({"obs:1": 100, "obs:2": None})
    assert source_evidence_status_for(conn_broken, ("obs:1", "obs:2")) is InputEvidenceStatus.REJECTED

    # Dereferenced pointers return VERIFIED
    conn_valid = _MockConn({"obs:1": 100, "obs:2": 101})
    assert source_evidence_status_for(conn_valid, ("obs:1", "obs:2")) is InputEvidenceStatus.VERIFIED

    # Unevidenced side payload returns DEGRADED
    unevidenced = {"headcount": "5000", "vintage": {}}
    assert (
        source_evidence_status_for(conn_valid, ("obs:1",), financial_payloads=[unevidenced])
        is InputEvidenceStatus.DEGRADED
    )


def test_status_dimensions_validation_status_honesty() -> None:
    """Factor validation defaults to not_evaluated in absence of sealed holdout record."""
    status = validation_status_for(("gppe-definition:" + "a" * 64,))
    assert status is FactorValidationStatus.NOT_EVALUATED


# ======================================================================================
# Pillar 4: Database Lineage & Schema Invariants (Live DB Integration)
# ======================================================================================


@pytest.fixture
def db_conn() -> Iterator[psycopg.Connection[Any]]:
    """Yield a connection inside a transaction rolled back after test execution."""
    if "DATABASE_URL" not in os.environ:
        pytest.skip("DATABASE_URL not set; skipping live Postgres integration test")

    url = os.environ["DATABASE_URL"]
    try:
        with psycopg.connect(url, autocommit=False) as conn:
            yield conn
            conn.rollback()
    except psycopg.OperationalError as error:
        pytest.skip(f"Live Postgres connection unavailable: {error}")


def test_pit_schema_ssot_no_clock_defaults_on_knowable_at(db_conn: psycopg.Connection[Any]) -> None:
    """staging.capture_normalized_observations.knowable_at must have NO default clock expression."""
    row = db_conn.execute(
        """
        select column_default
        from information_schema.columns
        where table_schema = 'staging'
          and table_name = 'capture_normalized_observations'
          and column_name = 'knowable_at'
        """
    ).fetchone()
    assert row is not None, "staging.capture_normalized_observations.knowable_at column must exist"
    assert row[0] is None, f"knowable_at must not have a default expression: {row[0]}"

    # Verify market prices daily transaction_time has NO default
    row_prices = db_conn.execute(
        """
        select column_default
        from information_schema.columns
        where table_schema = 'staging'
          and table_name = 'market_prices_daily'
          and column_name = 'transaction_time'
        """
    ).fetchone()
    assert row_prices is not None, "staging.market_prices_daily.transaction_time column must exist"
    assert row_prices[0] is None, f"market_prices_daily.transaction_time must not have a default: {row_prices[0]}"

    # Verify retired table staging.financial_facts does NOT exist
    regclass = db_conn.execute("select to_regclass('staging.financial_facts')").fetchone()
    assert regclass is not None
    assert regclass[0] is None, f"staging.financial_facts must be retired and absent: {regclass[0]}"


def test_pit_schema_constraint_recorded_at_must_be_after_knowable_at(db_conn: psycopg.Connection[Any]) -> None:
    """Inserting recorded_at earlier than knowable_at must raise check constraint violation."""
    future_knowable = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)
    past_recorded = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)

    with pytest.raises(psycopg.errors.CheckViolation, match="capture_normalized_observations_check"):
        db_conn.execute(
            """
            insert into staging.capture_normalized_observations (
                observation_id, content_sha256, capture_obligation_id,
                source_vintage_id, semantic_type, semantic_version,
                subject_kind, subject_id, valid_from, knowable_at,
                parser_version, mapping_version, normalized_payload_sha256,
                is_restatement, confidence, freshness_state, payload, recorded_at
            ) values (
                'normalized-observation:' || repeat('a', 64), repeat('a', 64),
                'list-obligation:' || repeat('b', 64),
                'source-vintage:' || repeat('c', 64),
                'financial-fact', 'v1', 'listing', 'listing:xnys:test',
                %s, %s, 'parser:v1', 'mapping:v1', repeat('d', 64),
                false, 0.9, 'fresh', '{}'::jsonb, %s
            )
            """,
            (past_recorded, future_knowable, past_recorded),
        )


def test_scope_ssot_mart_topt_capture_status_denominator_and_completion(db_conn: psycopg.Connection[Any]) -> None:
    """mart.topt_capture_status derives obligation_count from raw obligations and checks completion."""
    label = f"scope_test_{datetime.now(UTC).timestamp()}"
    universe = UniverseRef(
        universe_id=f"universe:{label}",
        universe_version="v1",
        content_sha256=canonical_sha256({"label": label}),
    )
    members = (
        SubjectRef(kind=SubjectKind.LISTING, id=f"listing:{label}:1"),
        SubjectRef(kind=SubjectKind.LISTING, id=f"listing:{label}:2"),
    )
    cutoff = datetime(2026, 4, 2, tzinfo=UTC)
    list_version = CaptureListVersion(universe=universe, members=members, effective_at=cutoff)
    policy = CaptureSchedulePolicy(
        policy_version=f"policy:{label}:v1",
        demanded_cadence=timedelta(days=1),
        provider_availability_cadence="manual-only:v1",
        freshness_max_age=timedelta(days=2),
        retry=replay_retry_policy(3),
    )
    campaign = CaptureCampaign(
        campaign_policy_id=f"campaign:{label}:v1",
        environment=CaptureEnvironment.PRODUCTION,
        cutoff=cutoff,
        universe_refs=(universe,),
    )
    repo = PostgresCaptureControlRepository(db_conn)
    repo.put_schedule_policy(policy)
    repo.put_campaign(campaign)
    repo.put_list_version(list_version)
    repo.bind_campaign_list(campaign.campaign_id, list_version.list_version_id)

    run = CaptureRun(
        campaign_id=campaign.campaign_id,
        run_sequence=1,
        schedule_policy_id=policy.schedule_policy_id,
        capture_scope_id=f"capture-scope:{canonical_sha256({'label': label})}",
    )
    repo.put_run(run)

    obligations = expand_obligations(
        run_id=run.run_id,
        list_version=list_version,
        semantic_types=("market-price", "financial-fact"),
        partition="2026-04-01",
    )
    for ob in obligations:
        repo.put_obligation(campaign.campaign_id, ob)

    # Before results are inserted: obligation_count == 4, terminal_count == 0, complete == false
    status = db_conn.execute(
        "select obligation_count, terminal_count, complete from mart.topt_capture_status where run_id = %s",
        (run.run_id,),
    ).fetchone()
    assert status is not None
    assert status[0] == 4, "obligation_count must equal total planned obligations"
    assert status[1] == 0, "terminal_count must be 0 before results land"
    assert status[2] is False, "complete must be False when obligations lack terminal results"


def test_5_hop_backward_lineage_trace_from_mart_to_s3_bytes(db_conn: psycopg.Connection[Any]) -> None:
    """Downstream mart results trace back through 5 hops to raw S3 bytes and SEC accession."""
    # 1. Hop 5 Physical simulation: Raw source bytes in S3
    raw_payload_bytes = b'{"cik": "0000320193", "entityName": "Apple Inc.", "facts": {"us-gaap": {"Revenues": {}}}}'
    raw_payload_sha256 = hashlib.sha256(raw_payload_bytes).hexdigest()
    sec_accession = "0000320193-23-000106"
    s3_uri = f"s3://truealpha-raw/sec/0000320193/{sec_accession}.json"
    knowable_at = datetime(2026, 4, 1, 16, 0, tzinfo=UTC)
    recorded_at = datetime(2026, 4, 1, 17, 0, tzinfo=UTC)

    # 2. Hop 4: Insert raw.fetches
    fetch_id = db_conn.execute(
        """
        insert into raw.fetches (
            source, source_record_id, payload_sha256, object_uri,
            content_type, byte_length, source_published_at, fetched_at, recorded_at
        ) values (
            'sec', %s, %s, %s,
            'application/json', %s, %s, %s, %s
        ) returning id
        """,
        (
            sec_accession,
            raw_payload_sha256,
            s3_uri,
            len(raw_payload_bytes),
            knowable_at,
            recorded_at,
            recorded_at,
        ),
    ).fetchone()[0]

    # 3. Hop 3: Insert raw.capture_source_requests and raw.capture_source_vintages
    request_id = f"source-request:{canonical_sha256({'trace': 'req'})}"
    db_conn.execute(
        """
        insert into raw.capture_source_requests (
            source_request_id, content_sha256, source_registry_entry_id,
            source_policy_id, request_fingerprint_version,
            canonical_request_sha256, subject_refs, capture_requirement_ids,
            partition_key, payload
        ) values (
            %s, %s, %s,
            'sec-policy:v1', 'sec:v1',
            %s, %s::jsonb, array['financial-fact:v1'],
            '2026-04-01', '{}'::jsonb
        )
        """,
        (
            request_id,
            canonical_sha256({"trace": "req"}),
            registry.registration_for("financial-fact").entry_id,
            canonical_sha256({"canon": "trace"}),
            json.dumps([{"kind": "listing", "id": "listing:xnys:aapl"}]),
        ),
    )

    vintage_id = f"source-vintage:{canonical_sha256({'trace': 'vin'})}"
    db_conn.execute(
        """
        insert into raw.capture_source_vintages (
            source_vintage_id, content_sha256, source_request_id,
            source_record_id, source_published_at, raw_object_id,
            raw_fetch_id, payload
        ) values (
            %s, %s, %s,
            %s, %s, %s,
            %s, '{}'::jsonb
        )
        """,
        (
            vintage_id,
            canonical_sha256({"trace": "vin"}),
            request_id,
            sec_accession,
            knowable_at,
            f"raw-object:{raw_payload_sha256}",
            fetch_id,
        ),
    )

    # 4. Hop 2: Insert capture control chain and staging.capture_normalized_observations
    label = f"trace_{datetime.now(UTC).timestamp()}"
    universe = UniverseRef(
        universe_id=f"universe:{label}",
        universe_version="v1",
        content_sha256=canonical_sha256({"trace": label}),
    )
    list_version = CaptureListVersion(
        universe=universe,
        members=(SubjectRef(kind=SubjectKind.LISTING, id="listing:xnys:aapl"),),
        effective_at=knowable_at,
    )
    policy = CaptureSchedulePolicy(
        policy_version=f"policy:{label}:v1",
        demanded_cadence=timedelta(days=1),
        provider_availability_cadence="manual-only:v1",
        freshness_max_age=timedelta(days=2),
        retry=replay_retry_policy(3),
    )
    campaign = CaptureCampaign(
        campaign_policy_id=f"campaign:{label}:v1",
        environment=CaptureEnvironment.PRODUCTION,
        cutoff=knowable_at,
        universe_refs=(universe,),
    )
    repo = PostgresCaptureControlRepository(db_conn)
    repo.put_schedule_policy(policy)
    repo.put_campaign(campaign)
    repo.put_list_version(list_version)
    repo.bind_campaign_list(campaign.campaign_id, list_version.list_version_id)
    run = CaptureRun(
        campaign_id=campaign.campaign_id,
        run_sequence=1,
        schedule_policy_id=policy.schedule_policy_id,
        capture_scope_id=f"capture-scope:{canonical_sha256({'trace': label})}",
    )
    repo.put_run(run)
    obligations = expand_obligations(
        run_id=run.run_id,
        list_version=list_version,
        semantic_types=("financial-fact",),
        partition="2026-04-01",
    )
    repo.put_obligation(campaign.campaign_id, obligations[0])

    observation_id = f"normalized-observation:{canonical_sha256({'trace': 'obs'})}"
    db_conn.execute(
        """
        insert into staging.capture_normalized_observations (
            observation_id, content_sha256, capture_obligation_id,
            source_vintage_id, semantic_type, semantic_version,
            subject_kind, subject_id, valid_from, knowable_at,
            parser_version, mapping_version, normalized_payload_sha256,
            is_restatement, confidence, freshness_state, payload, recorded_at
        ) values (
            %s, %s, %s,
            %s, 'financial-fact', 'v1',
            'listing', 'listing:xnys:aapl', %s, %s,
            'sec-parser:v1', 'mapping:v1', %s,
            false, 0.95, 'fresh', '{}'::jsonb, %s
        )
        """,
        (
            observation_id,
            canonical_sha256({"trace": "obs"}),
            obligations[0].obligation_id,
            vintage_id,
            knowable_at,
            knowable_at,
            canonical_sha256({"payload": "obs"}),
            recorded_at,
        ),
    )

    # 5. Hop 1: Insert mart.topt_core_results
    result_sha = canonical_sha256({"trace": "mart_core_result"})
    result_id = f"topt-core-result:{result_sha}"
    db_conn.execute("set local session_replication_role = replica")
    db_conn.execute(
        """
        insert into mart.topt_core_results (
            result_id, content_sha256, invocation_id, snapshot_id, run_id,
            release_manifest_id, universe_id, universe_version, universe_sha256,
            cutoff, issuer_id, instrument_id, listing_id, operating_branch,
            operating_metric, availability, operating_efficiency,
            capital_adjusted_gross_profit, gppe, tier, target_ps_lower,
            target_ps_upper, target_ps_midpoint, current_ps, valuation_gap,
            confidence, freshness, reason_codes, input_observation_ids,
            gppe_invocation_id, gppe_result_id, gppe_definition_id,
            gppe_definition_sha256, tier_definition_id, tier_definition_sha256,
            payload
        ) values (
            %s, %s, %s, %s, %s,
            %s, 'universe:trace', 'v1', %s,
            %s, 'issuer:aapl', 'instrument:aapl', 'listing:xnys:aapl', 'non_financial',
            'capital_adjusted_gppe', 'available', 1200.0,
            60000.0, 30.0, 'tech', 2.0,
            4.0, 3.0, 2.5, 0.5,
            0.92, 'fresh', '{}', array[%s, %s, %s, %s],
            %s, %s, 'gppe-def:v1',
            %s, 'tier-def:v1', %s,
            '{}'::jsonb
        )
        """,
        (
            result_id,
            result_sha,
            f"topt-core-invocation:{'1' * 64}",
            f"topt-core-snapshot:{'2' * 64}",
            run.run_id,
            f"release-manifest:{'3' * 64}",
            canonical_sha256({"trace": "universe"}),
            knowable_at,
            observation_id,
            observation_id,
            observation_id,
            observation_id,
            f"gppe-inv:{'4' * 64}",
            f"gppe-res:{'5' * 64}",
            canonical_sha256({"gppe": "v1"}),
            canonical_sha256({"tier": "v1"}),
        ),
    )

    # 6. Backward 5-Hop Query Verification
    traversal_query = """
        select
            m.result_id,
            m.listing_id,
            m.cutoff,
            obs.observation_id,
            obs.knowable_at,
            obs.recorded_at,
            v.source_vintage_id,
            v.source_record_id as vintage_accession,
            f.id as fetch_id,
            f.source,
            f.source_record_id as fetch_accession,
            f.object_uri,
            f.payload_sha256,
            f.byte_length
        from mart.topt_core_results m
        cross join lateral unnest(m.input_observation_ids) as input_obs(obs_id)
        join staging.capture_normalized_observations obs on obs.observation_id = input_obs.obs_id
        join raw.capture_source_vintages v on v.source_vintage_id = obs.source_vintage_id
        join raw.fetches f on f.id = v.raw_fetch_id
        where m.result_id = %s
    """
    rows = db_conn.execute(traversal_query, (result_id,)).fetchall()
    assert len(rows) == 4, "Must retrieve all 4 input observation lineage hops"

    row = rows[0]
    # Mart assertions
    assert row[0] == result_id
    assert row[1] == "listing:xnys:aapl"
    assert row[2] == knowable_at

    # Staging observation assertions (PIT)
    assert row[3] == observation_id
    assert row[4] == knowable_at
    assert row[5] == recorded_at
    assert row[5] >= row[4], "recorded_at must be strictly greater than or equal to knowable_at"

    # Vintage assertions
    assert row[6] == vintage_id
    assert row[7] == sec_accession

    # Raw fetch assertions
    assert row[8] == fetch_id
    assert row[9] == "sec"
    assert row[10] == sec_accession
    assert row[11] == s3_uri
    assert row[11].startswith("s3://")
    assert row[12] == raw_payload_sha256
    assert row[13] == len(raw_payload_bytes)


def test_raw_vintage_lineage_trigger_enforces_byte_integrity(db_conn: psycopg.Connection[Any]) -> None:
    """raw.capture_source_vintages rejects insert if raw_object_id != raw.fetches.payload_sha256."""
    # Create fetch
    payload_sha = canonical_sha256({"byte": "real"})
    fetch_id = db_conn.execute(
        """
        insert into raw.fetches (
            source, source_record_id, payload_sha256, object_uri,
            content_type, byte_length, fetched_at
        ) values (
            'sec', '0000320193-23-000999', %s, 's3://bucket/test.json',
            'application/json', 100, now()
        ) returning id
        """,
        (payload_sha,),
    ).fetchone()[0]

    req_id = f"source-request:{canonical_sha256({'trigger': 'req'})}"
    db_conn.execute(
        """
        insert into raw.capture_source_requests (
            source_request_id, content_sha256, source_registry_entry_id,
            source_policy_id, request_fingerprint_version,
            canonical_request_sha256, subject_refs, capture_requirement_ids,
            partition_key, payload
        ) values (
            %s, %s, %s,
            'p:v1', 'sec:v1',
            %s, '[{"kind":"listing","id":"listing:test"}]'::jsonb, array['financial-fact:v1'],
            '2026-04-01', '{}'::jsonb
        )
        """,
        (
            req_id,
            canonical_sha256({"trigger": "req"}),
            registry.registration_for("financial-fact").entry_id,
            canonical_sha256({"canon": "trigger"}),
        ),
    )

    # Insert with mismatched raw_object_id
    mismatched_sha = canonical_sha256({"byte": "corrupt_fake"})
    with pytest.raises(
        psycopg.errors.CheckViolation, match="capture source vintage raw lineage does not match landed bytes"
    ):
        db_conn.execute(
            """
            insert into raw.capture_source_vintages (
                source_vintage_id, content_sha256, source_request_id,
                source_record_id, raw_object_id, raw_fetch_id, payload
            ) values (
                'source-vintage:' || repeat('9', 64), repeat('9', 64),
                %s, '0000320193-23-000999',
                'raw-object:' || %s,
                %s, '{}'::jsonb
            )
            """,
            (req_id, mismatched_sha, fetch_id),
        )
