from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import cast

import dagster as dg
import psycopg
import pytest
from data_engine.batches.mvp_medium_validation.e0_slice import build_price_registry
from data_engine.batches.mvp_medium_validation.e2_slice import (
    D1_GOVERNANCE_HANDOFF_ID,
    D1_GOVERNANCE_HANDOFF_SHA256,
    D1_RUNTIME_HANDOFF_ID,
    D1_RUNTIME_HANDOFF_SHA256,
    D2_E2_ASSET_NAME,
    D2E2Activation,
    _module_sha256,
    _verify_d1_governance_handoff,
    build_d2_e2_definitions,
)
from data_engine.config import settings
from data_engine.mvp_medium_registry import build_medium_registry
from data_engine.mvp_medium_repository import (
    PostgresMediumSemanticRepository,
    build_medium_repository_registrations,
)
from pydantic import ValidationError
from truealpha_contracts import RawIngestionEnvelope, RawObjectRef
from truealpha_contracts.release import ReleaseManifest

REPOSITORY_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "governance" / "vision-issue-graph.json").is_file()
)
SQL_CONTRACT_PATH = REPOSITORY_ROOT / "db/tests/mvp_medium_validation_contract.sql"


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


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def test_e2_dagster_handoff_is_complete_stable_and_idempotent(connection) -> None:
    store = MemoryRawObjectStore()
    definitions = build_d2_e2_definitions(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=store,
        activation=D2E2Activation(environment="ci"),
    )
    dg.Definitions.validate_loadable(definitions)

    first_result = definitions.get_implicit_global_asset_job_def().execute_in_process()
    second_result = definitions.get_implicit_global_asset_job_def().execute_in_process()
    first = first_result.output_for_node(D2_E2_ASSET_NAME)
    second = second_result.output_for_node(D2_E2_ASSET_NAME)

    assert first_result.success and second_result.success
    assert first == second
    assert first.handoff_id == f"mvp-medium-validation-handoff:{first.content_sha256}"
    assert first.d1_governance_handoff_id == D1_GOVERNANCE_HANDOFF_ID
    assert first.d1_governance_handoff_sha256 == D1_GOVERNANCE_HANDOFF_SHA256
    assert first.d1_runtime_handoff_id == D1_RUNTIME_HANDOFF_ID
    assert first.d1_runtime_handoff_sha256 == D1_RUNTIME_HANDOFF_SHA256
    assert first.allowed_consumers == ("D2-mvp-medium-validation",)
    assert first.allowed_environments == ("ci", "local")
    assert "raw.fetches:" not in first.model_dump_json()
    assert len(first.normalized_record_ids) == 213
    assert {table: len(ids) for table, ids in first.projection_record_ids.items()} == {
        "staging.filing_documents": 2,
        "staging.mvp_corporate_actions": 2,
        "staging.mvp_financial_facts": 2,
        "staging.mvp_issuer_security_links": 1,
        "staging.mvp_market_prices": 2,
        "staging.mvp_security_listing_links": 1,
        "staging.mvp_universe_memberships": 203,
    }

    snapshots = first.snapshot_bundle.snapshots
    assert len(snapshots) == 5
    assert len({snapshot.request.valid_on for snapshot in snapshots}) == 4
    assert {len(snapshot.universe_memberships) for snapshot in snapshots if snapshot.universe_manifest is not None} == {
        101,
        102,
    }
    assert all(
        record.draft.semantic_type_id != "semantic.corporate-action"
        for snapshot in snapshots
        for record in snapshot.normalized_records
    )
    assert all(action.raw_ref.startswith("raw-object:") for action in first.market_event_bundle.actions)

    repository = PostgresMediumSemanticRepository(
        connection,
        registry=first.registry_snapshot,
        registrations=build_medium_repository_registrations(first.registry_snapshot),
    )
    projected = repository.project(
        first.market_event_bundle.normalized_records[0].normalized_record_id,
        as_of=first.created_at,
    ).model_dump(mode="json")
    assert set(projected) == {
        "as_of",
        "confidence",
        "payload_model_key",
        "payload_sha256",
        "subject",
        "valid_from",
        "valid_to",
    }
    assert not {"lineage", "raw_ref", "registry", "source"} & projected.keys()


def test_e2_registry_extends_d1_and_price_without_mutating_entries() -> None:
    price = build_price_registry()
    medium, history = build_medium_registry(
        price,
        source_implementation_sha256=_module_sha256(),
    )

    assert len(history.snapshots) == 3
    assert history.snapshots[1] == price
    assert medium.parent_snapshot_id == price.registry_snapshot_id
    medium_sources = {entry.key: entry for entry in medium.sources}
    medium_types = {entry.key: entry for entry in medium.semantic_types}
    assert {entry.key: medium_sources[entry.key] for entry in price.sources} == {
        entry.key: entry for entry in price.sources
    }
    assert {entry.key: medium_types[entry.key] for entry in price.semantic_types} == {
        entry.key: entry for entry in price.semantic_types
    }
    assert len(medium.sources) == 6
    assert len(medium.semantic_types) == 7
    assert len(build_medium_repository_registrations(medium)) == 7


def test_e2_rejects_staging_release_wrong_parent_and_mutated_d1(connection, tmp_path) -> None:
    with pytest.raises(ValidationError, match="environment"):
        D2E2Activation.model_validate({"environment": "staging"})
    with pytest.raises(ValidationError, match="accepted D1 runtime handoff"):
        D2E2Activation(
            environment="ci",
            expected_d1_handoff_id="mvp-normalization-handoff:" + "0" * 64,
            expected_d1_handoff_sha256="0" * 64,
        )
    with pytest.raises(ValueError, match="explicit Local/CI activation"):
        build_d2_e2_definitions(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=MemoryRawObjectStore(),
            activation=cast(ReleaseManifest, object()),
        )

    copied = tmp_path / "governance/handoffs"
    copied.mkdir(parents=True)
    handoff = (REPOSITORY_ROOT / "governance/handoffs/D1-mvp-normalization-handoff.v1.json").read_text()
    (copied / "D1-mvp-normalization-handoff.v1.json").write_text(
        handoff.replace('"state": "accepted"', '"state": "revoked"', 1)
    )
    with pytest.raises(ValueError, match="bytes drifted"):
        _verify_d1_governance_handoff(tmp_path)


def test_e2_sql_contract_executes() -> None:
    completed = subprocess.run(
        [
            "psql",
            settings.database_url,
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            str(SQL_CONTRACT_PATH),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
