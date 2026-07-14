from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import cast

import dagster as dg
import psycopg
import pytest
from data_engine.batches.mvp_medium_validation import e3_slice
from data_engine.batches.mvp_medium_validation.e3_slice import (
    D2_E2_RUNTIME_HANDOFF_ID,
    D2_E3_ASSET_NAME,
    D2E3Activation,
    D2E3RowCompleteManifest,
    FrozenToptDenominator,
    FrozenToptMarketFixture,
    build_d2_e3_definitions,
    load_topt_denominator,
    load_topt_market_fixture,
    run_d2_e3,
)
from data_engine.config import settings
from data_engine.mvp_medium_pipeline import LandedMediumCapture
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import ValidationError
from truealpha_contracts import RawIngestionEnvelope, RawObjectRef
from truealpha_contracts.release import ReleaseManifest

REPOSITORY_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "governance" / "vision-issue-graph.json").is_file()
)


class MemoryRawObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def store(self, capture) -> RawIngestionEnvelope:
        sha256 = hashlib.sha256(capture.body).hexdigest()
        ref = RawObjectRef(
            bucket="d2-fixtures",
            key=sha256,
            sha256=sha256,
            byte_length=len(capture.body),
            content_type=capture.content_type,
        )
        existing = self.objects.setdefault(ref.uri, capture.body)
        if existing != capture.body:
            raise ValueError("content-addressed raw object collision")
        return RawIngestionEnvelope(
            source=capture.source,
            source_record_id=capture.source_record_id,
            object=ref,
            fetched_at=capture.fetched_at,
            source_published_at=capture.source_published_at,
            metadata=capture.metadata,
        )

    def get(self, ref: RawObjectRef) -> bytes:
        body = self.objects[ref.uri]
        if hashlib.sha256(body).hexdigest() != ref.sha256:
            raise ValueError("raw object checksum mismatch")
        return body


@pytest.fixture(scope="module")
def e3_database_url():
    parameters = conninfo_to_dict(settings.database_url)
    database_name = f"truealpha_d2_e3_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    admin_url = make_conninfo(**(parameters | {"dbname": "postgres"}))
    target_url = make_conninfo(**(parameters | {"dbname": database_name}))
    try:
        with psycopg.connect(admin_url, connect_timeout=3, autocommit=True) as admin:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(database_name)))
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        for migration in (*sorted((REPOSITORY_ROOT / "db/migrations").glob("*.sql")), REPOSITORY_ROOT / "db/roles.sql"):
            completed = subprocess.run(
                ["psql", target_url, "-v", "ON_ERROR_STOP=1", "-f", str(migration)],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                pytest.fail(completed.stdout + completed.stderr, pytrace=False)
        yield target_url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s",
                (database_name,),
            )
            admin.execute(sql.SQL("drop database if exists {}").format(sql.Identifier(database_name)))


@pytest.fixture
def connection(e3_database_url):
    active = psycopg.connect(e3_database_url, connect_timeout=3, autocommit=False)
    active.execute("begin")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def _count(connection, table: str) -> int:
    if table not in {
        "raw.fetches",
        "staging.normalized_records",
        "staging.mvp_issuer_security_links",
        "staging.mvp_market_prices",
        "staging.mvp_security_listing_links",
        "staging.mvp_universe_memberships",
    }:
        raise ValueError(table)
    row = connection.execute(f"select count(*) from {table}").fetchone()
    assert row is not None
    return cast(int, row[0])


def test_e3_dagster_run_is_exact_row_complete_and_idempotent(connection) -> None:
    connection.execute("set local time zone 'Asia/Singapore'")
    assert _count(connection, "staging.normalized_records") == 0
    definitions = build_d2_e3_definitions(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=MemoryRawObjectStore(),
        activation=D2E3Activation(environment="ci"),
    )
    dg.Definitions.validate_loadable(definitions)

    first_result = definitions.get_implicit_global_asset_job_def().execute_in_process()
    second_result = definitions.get_implicit_global_asset_job_def().execute_in_process()
    first = first_result.output_for_node(D2_E3_ASSET_NAME)
    second = second_result.output_for_node(D2_E3_ASSET_NAME)

    assert first_result.success and second_result.success
    assert first == second
    assert first.evidence_id == "d2-e3-evidence:e0268851482a397bd32fff242c81f968ae9a7d782f32b5b34980c992f5c3716e"
    assert first.accepted_e2_handoff_id == D2_E2_RUNTIME_HANDOFF_ID
    assert first.denominator.universe_id == "universe:topt-us-2026-03-31"
    assert first.denominator.accession == "000207169126012475"
    assert len({item.issuer_lei for item in first.denominator.instruments}) == 20
    assert len(first.denominator.instruments) == 21
    alphabet = {item.ticker: item for item in first.denominator.instruments if item.ticker in {"GOOG", "GOOGL"}}
    assert alphabet["GOOG"].issuer_lei == alphabet["GOOGL"].issuer_lei
    assert alphabet["GOOG"].cusip != alphabet["GOOGL"].cusip
    assert first.pre_knowable_rejected
    assert first.fixture_postgres_parity
    assert not first.release_allowed
    assert first.market_original_raw_sha256 == ("7a160dbd1a5816d0c31e20bd1f0e1ab8d2738e1fc744fc7bf96fa2903d19e038")
    assert first.market_changed_raw_sha256 != first.market_original_raw_sha256
    assert len(first.normalized_record_ids) == 168
    assert first.universe_manifest.ref.content_sha256 == (
        "0320f5d4d6284eb6c72d476e47dca87911c61c79527d14824f839d94c621b129"
    )
    assert len(first.universe_manifest.membership_ids) == 21
    assert len(first.snapshots) == 2
    assert {snapshot.request.registry_snapshot_id for snapshot in first.snapshots} == {
        "registry-snapshot:7cda61357952971bf892aa9871c61ae6d39c08989b75247e33e50e911c212ef5"
    }
    assert all(len(snapshot.universe_memberships) == 21 for snapshot in first.snapshots)
    assert all(len(snapshot.normalized_records) == 84 for snapshot in first.snapshots)
    assert all(len(snapshot.selections) == 84 for snapshot in first.snapshots)
    assert all(len(row.cells) == 84 for row in first.row_manifests)
    market_fixture = load_topt_market_fixture(REPOSITORY_ROOT, first.denominator)
    source_retrieved_at = max(
        market_fixture.listing_source.retrieved_at,
        market_fixture.price_source.retrieved_at,
    )
    assert all(
        record.draft.knowable_at >= source_retrieved_at
        for snapshot in first.snapshots
        for record in snapshot.normalized_records
        if record.draft.semantic_type_id in {"semantic.market-price", "semantic.security-listing-link"}
    )
    expected_type_counts = {
        "semantic.issuer-security-link": 21,
        "semantic.market-price": 21,
        "semantic.security-listing-link": 21,
        "semantic.universe-membership": 21,
    }
    assert all(
        Counter(cell.demand.semantic_type_id for cell in row.cells) == expected_type_counts
        for row in first.row_manifests
    )
    implementation_sha256 = hashlib.sha256(
        (REPOSITORY_ROOT / "apps/data-engine/src/data_engine/batches/mvp_medium_validation/e3_slice.py").read_bytes()
    ).hexdigest()
    expected_topt_sources = {
        "semantic.issuer-security-link": (
            "source-registry-entry:86641ce50d78bfc87d03cc0a4ab17230d04b1c3a7db964d10f39555e89d569ba"
        ),
        "semantic.market-price": "source-registry-entry:bf81b8356f25d7cda19b9037ecfe8617056cf9c84ec594ca054b604636f7a391",
        "semantic.security-listing-link": (
            "source-registry-entry:9a811571884d5434a14ec9f5a5ea24050a6c140e106871ffd9c057bac37777b3"
        ),
        "semantic.universe-membership": (
            "source-registry-entry:6d58ccbf2eec140b47126d11643bce8e8a8e4310ff26bc1bc2e6b858392f85d1"
        ),
    }
    records_by_type = {
        semantic_type_id: tuple(
            record
            for snapshot in first.snapshots
            for record in snapshot.normalized_records
            if record.draft.semantic_type_id == semantic_type_id
        )
        for semantic_type_id in expected_topt_sources
    }
    assert {
        semantic_type_id: tuple(sorted({record.source_registry_entry_id for record in records}))
        for semantic_type_id, records in records_by_type.items()
    } == {
        semantic_type_id: (source_registry_entry_id,)
        for semantic_type_id, source_registry_entry_id in expected_topt_sources.items()
    }
    for records in records_by_type.values():
        assert len(records) == 42
        assert {record.draft.producer_implementation_sha256 for record in records} == {implementation_sha256}
        assert {record.mapping_implementation_sha256 for record in records} == {implementation_sha256}
    persisted = connection.execute(
        """
        select semantic_type_id, count(*), count(raw_ref), count(confidence)
        from staging.normalized_records
        where normalized_record_id = any(%s)
        group by semantic_type_id
        order by semantic_type_id
        """,
        (list(first.normalized_record_ids),),
    ).fetchall()
    assert {
        semantic_type_id: (row_count, raw_lineage_count, confidence_count)
        for semantic_type_id, row_count, raw_lineage_count, confidence_count in persisted
    } == {semantic_type_id: (42, 42, 42) for semantic_type_id in expected_type_counts}
    assert connection.execute(
        """
        select count(distinct source_registry_entry_id)
        from staging.normalized_records
        where normalized_record_id = any(%s)
          and semantic_type_id = 'semantic.market-price'
        """,
        (list(first.normalized_record_ids),),
    ).fetchone() == (1,)
    assert first.row_manifests[0].expected_cell_ids == first.row_manifests[1].expected_cell_ids
    selected = [{cell.normalized_record_id for cell in row.cells} for row in first.row_manifests]
    assert selected[0].isdisjoint(selected[1])

    assert _count(connection, "staging.normalized_records") == 381
    assert _count(connection, "staging.mvp_issuer_security_links") == 43
    assert _count(connection, "staging.mvp_market_prices") == 44
    assert _count(connection, "staging.mvp_security_listing_links") == 43
    assert _count(connection, "staging.mvp_universe_memberships") == 245
    assert connection.execute("show timezone").fetchone() == ("UTC",)

    row_payload = first.row_manifests[0].model_dump(mode="python")
    missing = deepcopy(row_payload)
    missing["cells"] = missing["cells"][:-1]
    with pytest.raises(ValidationError, match="at least 84 items"):
        D2E3RowCompleteManifest.model_validate(missing)
    duplicate = deepcopy(row_payload)
    duplicate["cells"] = list(duplicate["cells"])
    duplicate["cells"][-1] = duplicate["cells"][0]
    with pytest.raises(ValidationError, match="missing or duplicate required cells"):
        D2E3RowCompleteManifest.model_validate(duplicate)


def test_e3_denominator_rejects_shrink_duplicate_drift_and_float() -> None:
    denominator = load_topt_denominator(REPOSITORY_ROOT)
    payload = denominator.model_dump(mode="python")

    shrink = deepcopy(payload)
    shrink["instruments"] = shrink["instruments"][:-1]
    shrink["selected_instrument_cusips"] = shrink["selected_instrument_cusips"][:-1]
    with pytest.raises(ValidationError, match="at least 21 items"):
        FrozenToptDenominator.model_validate(shrink)

    duplicate = deepcopy(payload)
    duplicate["instruments"] = list(duplicate["instruments"])
    duplicate["instruments"][-1] = duplicate["instruments"][0]
    with pytest.raises(ValidationError, match="21 distinct instruments"):
        FrozenToptDenominator.model_validate(duplicate)

    wrong_accession = deepcopy(payload)
    wrong_accession["accession"] = "000000000000000000"
    with pytest.raises(ValidationError, match="Input should be '000207169126012475'"):
        FrozenToptDenominator.model_validate(wrong_accession)

    float_weight = deepcopy(payload)
    float_weight["instruments"] = list(float_weight["instruments"])
    float_weight["instruments"][0]["filing_weight_percent"] = 1.5
    with pytest.raises(ValidationError, match="exact Decimal literals"):
        FrozenToptDenominator.model_validate(float_weight)

    schema_drift = deepcopy(payload)
    schema_drift["instruments"] = list(schema_drift["instruments"])
    schema_drift["instruments"][0]["vendor_symbol"] = "AAPL"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FrozenToptDenominator.model_validate(schema_drift)


def test_e3_market_fixture_rejects_shrink_duplicate_float_and_successor_leak() -> None:
    denominator = load_topt_denominator(REPOSITORY_ROOT)
    fixture = load_topt_market_fixture(REPOSITORY_ROOT, denominator)
    payload = fixture.model_dump(mode="python")

    shrink = deepcopy(payload)
    shrink["rows"] = shrink["rows"][:-1]
    with pytest.raises(ValidationError, match="at least 21 items"):
        FrozenToptMarketFixture.model_validate(shrink)

    duplicate = deepcopy(payload)
    duplicate["rows"] = list(duplicate["rows"])
    duplicate["rows"][-1] = duplicate["rows"][0]
    with pytest.raises(ValidationError, match="incomplete or duplicated"):
        FrozenToptMarketFixture.model_validate(duplicate)

    binary_float = deepcopy(payload)
    binary_float["rows"] = list(binary_float["rows"])
    binary_float["rows"][0]["close"] = 1.5
    with pytest.raises(ValidationError, match="exact Decimal literals"):
        FrozenToptMarketFixture.model_validate(binary_float)

    successor_leak = deepcopy(payload)
    successor_leak["rows"] = list(successor_leak["rows"])
    xom = next(row for row in successor_leak["rows"] if row["ticker"] == "XOM")
    xom["issuer_cik_at_report_date"] = "0002115436"
    with pytest.raises(ValidationError, match="post-report XOM successor"):
        FrozenToptMarketFixture.model_validate(successor_leak)


def test_e3_market_routes_reject_corrupted_landed_bytes_and_bind_implementations(connection) -> None:
    store = MemoryRawObjectStore()
    e2_handoff = e3_slice.run_d2_e2(REPOSITORY_ROOT, connection, store, environment="ci")
    denominator = load_topt_denominator(REPOSITORY_ROOT)
    fixture = load_topt_market_fixture(REPOSITORY_ROOT, denominator)
    registry = e3_slice._e3_registry(e2_handoff.registry_snapshot)
    catalog = e3_slice._e3_catalog(registry, denominator, fixture)
    implementation_sha256 = hashlib.sha256(
        (REPOSITORY_ROOT / "apps/data-engine/src/data_engine/batches/mvp_medium_validation/e3_slice.py").read_bytes()
    ).hexdigest()
    corrupt_body = b'{"schema_version": 1}'
    routes = (
        (e3_slice.TOPT_ISSUER_SOURCE_ID, "semantic.issuer-security-link"),
        (e3_slice.TOPT_LISTING_SOURCE_ID, "semantic.security-listing-link"),
        (e3_slice.TOPT_MEMBERSHIP_SOURCE_ID, "semantic.universe-membership"),
        (e3_slice.TOPT_PRICE_SOURCE_ID, "semantic.market-price"),
    )

    for fetch_id, (source_id, semantic_type_id) in enumerate(routes, start=1):
        source = catalog.source(source_id, "1.0.0")
        assert source.adapter_implementation_sha256 == implementation_sha256
        assert source.normalizer_implementation_sha256 == implementation_sha256
        capture = LandedMediumCapture(
            fetch_id=fetch_id,
            raw_ref=f"raw.fetches:{fetch_id}",
            raw_object_sha256=hashlib.sha256(corrupt_body).hexdigest(),
            source_id=source.source_id,
            source_version=source.version,
            source_registry_entry_id=source.source_registry_entry_id,
            source_registry_entry_sha256=source.content_sha256,
            semantic_type_ids=(semantic_type_id,),
            semantic_type_versions={semantic_type_id: "1.0.0"},
            source_record_id=f"corrupt:{source_id}",
            body=corrupt_body,
            content_type="application/json",
            source_published_at=fixture.price_source.retrieved_at,
            fetched_at=fixture.price_source.retrieved_at,
            recorded_at=fixture.price_source.retrieved_at,
        )
        with pytest.raises(ValueError, match="missing|Field required"):
            catalog.normalize(capture, semantic_type_id)


def test_e3_failure_rolls_back_and_retry_recovers_without_duplicates(connection) -> None:
    class InjectedFailure(RuntimeError):
        pass

    def fail_after_original(_point: str) -> None:
        raise InjectedFailure("simulated terminal interruption")

    store = MemoryRawObjectStore()
    with pytest.raises(InjectedFailure, match="terminal interruption"):
        run_d2_e3(
            REPOSITORY_ROOT,
            connection,
            store,
            environment="ci",
            failure_injector=fail_after_original,
        )
    assert _count(connection, "raw.fetches") == 0
    assert _count(connection, "staging.normalized_records") == 0

    recovered = run_d2_e3(REPOSITORY_ROOT, connection, store, environment="ci")
    repeated = run_d2_e3(REPOSITORY_ROOT, connection, store, environment="ci")
    assert recovered == repeated
    assert _count(connection, "staging.normalized_records") == 381


def test_e3_rows_remain_append_only(connection) -> None:
    # Keep the successful run inside the fixture-owned transaction so this
    # mutation test cannot contaminate later acceptance runs.
    assert _count(connection, "staging.normalized_records") == 0
    evidence = run_d2_e3(
        REPOSITORY_ROOT,
        connection,
        MemoryRawObjectStore(),
        environment="ci",
    )
    record_id = evidence.normalized_record_ids[0]
    with pytest.raises(psycopg.errors.RaiseException, match="point-in-time records are append-only"):
        with connection.transaction():
            connection.execute(
                "update staging.normalized_records set confidence = 0.5 where normalized_record_id = %s",
                (record_id,),
            )
    with pytest.raises(psycopg.errors.RaiseException, match="point-in-time records are append-only"):
        with connection.transaction():
            connection.execute(
                "delete from staging.normalized_records where normalized_record_id = %s",
                (record_id,),
            )


def test_e3_rejects_staging_release_and_wrong_handoff_activation(connection) -> None:
    with pytest.raises(ValidationError):
        D2E3Activation(environment="staging")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        D2E3Activation(
            environment="ci",
            expected_e2_handoff_id="mvp-medium-validation-handoff:" + "0" * 64,
        )
    with pytest.raises(ValueError, match="explicit Local/CI activation"):
        build_d2_e3_definitions(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=MemoryRawObjectStore(),
            activation=cast(ReleaseManifest, object()),
        )
