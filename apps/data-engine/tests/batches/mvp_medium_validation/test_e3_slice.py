from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
from copy import deepcopy
from pathlib import Path
from typing import cast

import dagster as dg
import psycopg
import pytest
from data_engine.batches.mvp_medium_validation.e3_slice import (
    D2_E2_RUNTIME_HANDOFF_ID,
    D2_E3_ASSET_NAME,
    D2E3Activation,
    D2E3RowCompleteManifest,
    FrozenToptDenominator,
    build_d2_e3_definitions,
    load_topt_denominator,
    run_d2_e3,
)
from data_engine.config import settings
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
        "staging.mvp_universe_memberships",
    }:
        raise ValueError(table)
    row = connection.execute(f"select count(*) from {table}").fetchone()
    assert row is not None
    return cast(int, row[0])


def test_e3_dagster_run_is_exact_row_complete_and_idempotent(connection) -> None:
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
    assert first.evidence_id == "d2-e3-evidence:c15eb5f4f4361e7e020b53e46298945d2a488f9554b2297613ac0f2b41c27b63"
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
    assert len(first.normalized_record_ids) == 84
    assert first.universe_manifest.ref.content_sha256 == (
        "ad110f24bd26e40fe4b355b951fe3545fd7505b587051a07b9874d9419d7f057"
    )
    assert len(first.universe_manifest.membership_ids) == 21
    assert len(first.snapshots) == 2
    assert all(len(snapshot.universe_memberships) == 21 for snapshot in first.snapshots)
    assert all(len(snapshot.normalized_records) == 42 for snapshot in first.snapshots)
    assert all(len(snapshot.selections) == 42 for snapshot in first.snapshots)
    assert all(len(row.cells) == 42 for row in first.row_manifests)
    assert first.row_manifests[0].expected_cell_ids == first.row_manifests[1].expected_cell_ids
    selected = [{cell.normalized_record_id for cell in row.cells} for row in first.row_manifests]
    assert selected[0].isdisjoint(selected[1])

    assert _count(connection, "staging.normalized_records") == 297
    assert _count(connection, "staging.mvp_issuer_security_links") == 43
    assert _count(connection, "staging.mvp_universe_memberships") == 245

    row_payload = first.row_manifests[0].model_dump(mode="python")
    missing = deepcopy(row_payload)
    missing["cells"] = missing["cells"][:-1]
    with pytest.raises(ValidationError, match="at least 42 items"):
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
    assert _count(connection, "staging.normalized_records") == 297


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
