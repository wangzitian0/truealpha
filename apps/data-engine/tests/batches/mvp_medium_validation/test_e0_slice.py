from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import dagster as dg
import psycopg
import pytest
from data_engine.batches.mvp_medium_validation.e0_slice import (
    BATCH_MANIFEST_PATH,
    CORPUS_PATH,
    D2_E0_ASSET_NAME,
    SEMANTIC_TYPE_ID,
    SOURCE_ID,
    SUBJECT,
    VERSION,
    D2E0Activation,
    FrozenPriceAdapter,
    MarketPricePayload,
    PostgresMarketPriceRepository,
    PriceComponentCatalog,
    build_d2_e0_definitions,
    build_price_registry,
    build_price_snapshot,
    run_price_pipeline,
)
from data_engine.config import settings
from data_engine.mvp_registry import build_filing_registry
from pydantic import ValidationError
from truealpha_contracts import RawIngestionEnvelope, RawObjectRef
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.market import PriceBasis
from truealpha_contracts.release import ArtifactRole, ReleaseArtifact, ReleaseManifest
from truealpha_contracts.universe import UniverseRef

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


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    with active:
        yield active


def _changed_artifact():
    case = FrozenPriceAdapter().load(REPOSITORY_ROOT, environment="ci")
    original = case.artifact
    source_row = original.source_row.encode()
    changed_source_row = (
        b"2026-03-31,166.97000122070312,174.6199951171875,166.9600067138672,"
        b"174.52000000000000,174.31685974121094,226181300"
    )
    if original.body.count(source_row) != 1:
        raise AssertionError("frozen source row must occur exactly once")
    body = original.body.replace(source_row, changed_source_row)
    sha256 = hashlib.sha256(body).hexdigest()
    delta = timedelta(days=1)
    payload = MarketPricePayload.model_validate(
        {
            **original.payload.model_dump(mode="json"),
            "close": "174.52000000000000",
            "knowable_at": original.payload.knowable_at + delta,
            "produced_at": original.payload.produced_at + delta,
            "recorded_at": original.payload.recorded_at + delta,
        }
    )
    reconciliation = original.reconciliation.model_copy(
        update={
            "source_adjusted_close": Decimal("174.31685974121094"),
            "first_observed_at": payload.knowable_at,
            "raw_object_id": f"raw-object:{sha256}",
            "raw_object_sha256": sha256,
        }
    )
    return replace(
        original,
        artifact_id="nvda-daily-prices-v2",
        body=body,
        sha256=sha256,
        source_row=changed_source_row.decode(),
        payload=payload,
        reconciliation=reconciliation,
        supersedes_artifact_id=original.artifact_id,
    )


def test_dagster_e0_is_idempotent_and_matches_postgres_snapshot(connection) -> None:
    store = MemoryRawObjectStore()
    definitions = build_d2_e0_definitions(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=store,
        activation=D2E0Activation(environment="ci"),
    )
    dg.Definitions.validate_loadable(definitions)

    first = definitions.get_implicit_global_asset_job_def().execute_in_process()
    repeated = definitions.get_implicit_global_asset_job_def().execute_in_process()
    evidence = first.output_for_node(D2_E0_ASSET_NAME)
    repeated_evidence = repeated.output_for_node(D2_E0_ASSET_NAME)

    assert first.success and repeated.success
    assert evidence == repeated_evidence
    assert evidence.fixture_snapshot_id == evidence.postgres_snapshot_id
    assert evidence.fixture_runner_selection_id == evidence.postgres_runner_selection_id
    assert evidence.row_counts == {
        "normalized_records": 1,
        "raw_fetches": 1,
        "typed_price_payloads": 1,
    }
    assert evidence.stable_handoff is False


def test_typed_unadjusted_price_and_factor_projection_are_exact(connection) -> None:
    pipeline = run_price_pipeline(REPOSITORY_ROOT, connection, MemoryRawObjectStore(), environment="ci")
    payload = pipeline.payloads[0]
    price_bar = pipeline.price_bars[0]
    reconciliation = pipeline.artifacts[0].reconciliation
    bundle = build_price_snapshot(
        case=pipeline.case,
        records=pipeline.records,
        selected_record=pipeline.records[0],
        registry=pipeline.registry,
        environment="ci",
    )

    assert payload.listing_id == "listing:xnas:nvda"
    assert payload.trading_date.isoformat() == "2026-03-31"
    assert payload.open == Decimal("166.97000122070312")
    assert payload.high == Decimal("174.6199951171875")
    assert payload.low == Decimal("166.9600067138672")
    assert payload.close == Decimal("174.39999389648438")
    assert payload.volume == 226181300
    assert payload.confidence == Decimal("0.99")
    assert price_bar.price_basis is PriceBasis.UNADJUSTED
    assert reconciliation.source_adjusted_close == Decimal("174.1969757080078")
    assert reconciliation.factor_visible is False
    assert "adjusted" not in payload.model_dump(mode="json")
    assert bundle.evaluation.ready

    observation = bundle.runner_selection.factor_inputs[0].observation.model_dump(mode="json")
    corpus = json.loads((REPOSITORY_ROOT / CORPUS_PATH).read_text(encoding="utf-8"))
    assert set(observation) == set(corpus["cases"][0]["expected"]["factor_projection_fields"])
    assert not set(observation) & {"source", "raw_ref", "raw_object_id", "registry", "lineage"}
    assert pipeline.registry.parent_snapshot_id == build_filing_registry().registry_snapshot_id


def test_pit_before_at_and_stale_cutoffs_are_enforced(connection) -> None:
    pipeline = run_price_pipeline(REPOSITORY_ROOT, connection, MemoryRawObjectStore(), environment="ci")
    source = next(entry for entry in pipeline.registry.sources if entry.key == (SOURCE_ID, VERSION))
    repository = PostgresMarketPriceRepository(connection)
    assert (
        repository.select_pit(
            subject=SUBJECT,
            semantic_type_id=SEMANTIC_TYPE_ID,
            semantic_type_version=VERSION,
            source_registry_entry_id=source.source_registry_entry_id,
            valid_on=pipeline.payloads[0].trading_date,
            as_of=pipeline.case.before_knowable_at,
        )
        == ()
    )
    assert (
        repository.select_pit(
            subject=SUBJECT,
            semantic_type_id=SEMANTIC_TYPE_ID,
            semantic_type_version=VERSION,
            source_registry_entry_id=source.source_registry_entry_id,
            valid_on=pipeline.payloads[0].trading_date,
            as_of=pipeline.case.at_knowable_at,
        )
        == pipeline.records
    )
    stale = build_price_snapshot(
        case=pipeline.case,
        records=pipeline.records,
        selected_record=pipeline.records[0],
        registry=pipeline.registry,
        environment="ci",
        as_of=pipeline.case.stale_as_of,
    )
    assert not stale.evaluation.ready
    assert any("stale" in reason for reason in stale.evaluation.blocking_reason_codes)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"open": 166.97}, "binary floats"),
        ({"confidence": 0.99}, "binary floats"),
        ({"high": "100"}, "high is below"),
        ({"low": "170"}, "low is above"),
        ({"open": "NaN"}, "finite"),
        ({"price_basis": "adjusted_reconciliation_only"}, "unadjusted"),
    ],
)
def test_price_model_rejects_float_non_finite_malformed_and_adjusted_values(mutation, message) -> None:
    payload = FrozenPriceAdapter().load(REPOSITORY_ROOT, environment="ci").artifact.payload.model_dump(mode="json")
    payload.update(mutation)
    with pytest.raises(ValidationError, match=message):
        MarketPricePayload.model_validate(payload)


def test_price_model_requires_confidence_identity_lineage_and_time() -> None:
    case = FrozenPriceAdapter().load(REPOSITORY_ROOT, environment="ci")
    payload = case.artifact.payload.model_dump(mode="json")
    for missing in ("confidence", "issuer_id", "security_id", "listing_id", "knowable_at"):
        mutated = dict(payload)
        mutated.pop(missing)
        with pytest.raises(ValidationError):
            MarketPricePayload.model_validate(mutated)

    registry = build_price_registry()
    _, normalizer, source, semantic_type = PriceComponentCatalog.e0().resolve(registry)
    with pytest.raises(ValueError, match="raw lineage"):
        normalizer.normalize(case, case.artifact, 1, "0" * 64, source, semantic_type)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issuer_id", "issuer:lei:wrong"),
        ("security_id", "security:cusip:wrong"),
        ("listing_id", "listing:xnas:wrong"),
        ("share_class", "preferred"),
    ],
)
def test_normalizer_rejects_wrong_instrument_identity(field: str, value: str) -> None:
    case = FrozenPriceAdapter().load(REPOSITORY_ROOT, environment="ci")
    registry = build_price_registry()
    _, normalizer, source, semantic_type = PriceComponentCatalog.e0().resolve(registry)
    payload = case.artifact.payload.model_copy(update={field: value})
    artifact = replace(case.artifact, payload=payload)
    with pytest.raises(ValueError, match="identity"):
        normalizer.normalize(case, artifact, 1, artifact.sha256, source, semantic_type)


def test_identical_replay_and_changed_bytes_append_one_restatement(connection) -> None:
    case = FrozenPriceAdapter().load(REPOSITORY_ROOT, environment="ci")
    changed = _changed_artifact()

    class RollbackProbe(Exception):
        pass

    with pytest.raises(RollbackProbe), connection.transaction():
        first = run_price_pipeline(
            REPOSITORY_ROOT,
            connection,
            MemoryRawObjectStore(),
            environment="ci",
            artifacts=(changed, case.artifact),
        )
        repeated = run_price_pipeline(
            REPOSITORY_ROOT,
            connection,
            MemoryRawObjectStore(),
            environment="ci",
            artifacts=(case.artifact, changed),
        )
        assert first.records == repeated.records
        assert first.raw_fetch_ids == repeated.raw_fetch_ids
        assert first.records[1].is_restatement
        assert first.records[1].supersedes_record_id == first.records[0].normalized_record_id
        assert first.records[0].normalized_record_id != first.records[1].normalized_record_id

        repository = PostgresMarketPriceRepository(connection)
        source = next(entry for entry in first.registry.sources if entry.key == (SOURCE_ID, VERSION))
        assert repository.select_pit(
            subject=SUBJECT,
            semantic_type_id=SEMANTIC_TYPE_ID,
            semantic_type_version=VERSION,
            source_registry_entry_id=source.source_registry_entry_id,
            valid_on=first.payloads[0].trading_date,
            as_of=first.case.snapshot_as_of,
        ) == (first.records[0],)
        assert repository.select_pit(
            subject=SUBJECT,
            semantic_type_id=SEMANTIC_TYPE_ID,
            semantic_type_version=VERSION,
            source_registry_entry_id=source.source_registry_entry_id,
            valid_on=first.payloads[0].trading_date,
            as_of=first.payloads[1].recorded_at,
        ) == (first.records[1],)
        with pytest.raises(ValueError, match="future normalized record"):
            build_price_snapshot(
                case=first.case,
                records=first.records,
                selected_record=first.records[1],
                registry=first.registry,
                environment="ci",
                as_of=first.case.snapshot_as_of,
            )
        raise RollbackProbe


def test_raw_and_normalized_rows_are_append_only(connection) -> None:
    pipeline = run_price_pipeline(REPOSITORY_ROOT, connection, MemoryRawObjectStore(), environment="ci")
    statements = (
        ("update raw.fetches set metadata = metadata where id = %s", pipeline.raw_fetch_ids[0]),
        ("delete from raw.fetches where id = %s", pipeline.raw_fetch_ids[0]),
        (
            "update staging.normalized_records set confidence = confidence where normalized_record_id = %s",
            pipeline.records[0].normalized_record_id,
        ),
        (
            "delete from staging.normalized_records where normalized_record_id = %s",
            pipeline.records[0].normalized_record_id,
        ),
    )
    for statement, identifier in statements:
        with pytest.raises(psycopg.Error), connection.transaction():
            connection.execute(statement, (identifier,))


def test_registry_dispatch_is_dictionary_driven_and_unknown_components_fail() -> None:
    registry = build_price_registry()
    source = next(entry for entry in registry.sources if entry.key == (SOURCE_ID, VERSION))
    disabled = source.model_copy(update={"adapter_id": "batch:DisabledPriceAdapter"})
    drifted = registry.model_copy(
        update={"sources": tuple(disabled if entry.key == source.key else entry for entry in registry.sources)}
    )
    with pytest.raises(ValueError, match="not activated"):
        PriceComponentCatalog.e0().resolve(drifted)
    route_source = inspect.getsource(PriceComponentCatalog.resolve)
    assert "DataSource." not in route_source
    assert "match " not in route_source


def _accepted_release_manifest() -> ReleaseManifest:
    migration_ids = ("0001.sql",)
    return ReleaseManifest(
        contract_version="contracts:v1",
        mart_schema_version="mart:v1",
        research_catalog_id="research-catalog:" + "1" * 64,
        research_catalog_sha256="1" * 64,
        universe=UniverseRef(
            universe_id="universe:topt-test",
            universe_version="2026-07-12",
            content_sha256="2" * 64,
        ),
        capture_scope_id="capture-scope:" + "3" * 64,
        capture_scope_sha256="3" * 64,
        applicability_catalog_id="applicability:" + "4" * 64,
        applicability_catalog_sha256="4" * 64,
        source_coverage_catalog_id="source-coverage:" + "5" * 64,
        source_coverage_catalog_sha256="5" * 64,
        source_readiness_report_id="source-readiness:" + "6" * 64,
        source_readiness_report_sha256="6" * 64,
        slo_catalog_id="module-slo:" + "7" * 64,
        slo_catalog_sha256="7" * 64,
        consumer_slo_catalog_id="consumer-slo:" + "8" * 64,
        consumer_slo_catalog_sha256="8" * 64,
        usage_telemetry_slo_catalog_id="usage-telemetry-slo:" + "9" * 64,
        usage_telemetry_slo_catalog_sha256="9" * 64,
        registry_snapshot_id="registry-snapshot:" + "a" * 64,
        registry_snapshot_sha256="a" * 64,
        source_registry_id="source-registry:" + "b" * 64,
        source_registry_sha256="b" * 64,
        semantic_type_registry_id="semantic-type-registry:" + "c" * 64,
        semantic_type_registry_sha256="c" * 64,
        identifier_type_registry_id="identifier-type-registry:" + "d" * 64,
        identifier_type_registry_sha256="d" * 64,
        configuration_sha256={"data-engine": "e" * 64},
        migration_ids=migration_ids,
        migration_set_sha256=canonical_sha256(migration_ids),
        artifacts=tuple(
            ReleaseArtifact(
                role=role,
                image_or_bundle=f"ghcr.io/example/{role.value}",
                digest="sha256:" + "f" * 64,
                git_sha="0" * 40,
                sbom_sha256="1" * 64,
                signature_ref=f"sigstore:{role.value}",
            )
            for role in ArtifactRole
        ),
        natural_refresh_requirement_ids=("natural-refresh:" + "2" * 64,),
        created_at=datetime(2026, 7, 12, tzinfo=UTC),
        manifest_signature_ref="sigstore:accepted-release",
    )


def test_release_and_staging_activation_are_rejected(connection) -> None:
    with pytest.raises(ValidationError, match="environment"):
        D2E0Activation.model_validate({"environment": "staging"})
    with pytest.raises(ValueError, match="batch-private"):
        build_d2_e0_definitions(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=MemoryRawObjectStore(),
            activation=_accepted_release_manifest(),
        )


def _copy_frozen_tree(destination: Path) -> None:
    paths = (
        BATCH_MANIFEST_PATH,
        CORPUS_PATH,
        Path("governance/handoffs/D1-mvp-normalization-handoff.v1.json"),
        Path("governance/gate0/issue-59.research-semantics.candidate-v1.json"),
        Path("apps/data-engine/samples/capture_manifest_20260712.json"),
        Path("apps/data-engine/samples/prices/NVDA_prices_3y_20260712.csv"),
        Path("apps/data-engine/samples/filings/NVDA_8K_SPLIT_000104581024000144.html"),
    )
    for relative in paths:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPOSITORY_ROOT / relative, target)


def test_frozen_artifact_and_revoked_handoff_drift_fail_before_execution(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifact"
    _copy_frozen_tree(artifact_root)
    price_path = artifact_root / "apps/data-engine/samples/prices/NVDA_prices_3y_20260712.csv"
    price_path.write_bytes(price_path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="checksum drifted"):
        FrozenPriceAdapter().load(artifact_root, environment="ci")

    revoked_root = tmp_path / "revoked"
    _copy_frozen_tree(revoked_root)
    handoff_path = revoked_root / "governance/handoffs/D1-mvp-normalization-handoff.v1.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["state"] = "revoked"
    handoff["revocation"]["reason"] = "negative-control"
    handoff["revocation"]["revoked_at"] = "2026-07-13T00:00:00Z"
    handoff_path.write_text(json.dumps(handoff, indent=2) + "\n", encoding="utf-8")

    corpus_path = revoked_root / CORPUS_PATH
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    corpus["producer_handoff"]["sha256"] = hashlib.sha256(handoff_path.read_bytes()).hexdigest()
    corpus_path.write_text(json.dumps(corpus, indent=2) + "\n", encoding="utf-8")
    batch_path = revoked_root / BATCH_MANIFEST_PATH
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    batch["corpus"]["sha256"] = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    batch_path.write_text(json.dumps(batch, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not authorize"):
        FrozenPriceAdapter().load(revoked_root, environment="ci")


def test_manifest_corpus_registry_and_e0_claims_are_exact() -> None:
    manifest = json.loads((REPOSITORY_ROOT / BATCH_MANIFEST_PATH).read_text(encoding="utf-8"))
    corpus_bytes = (REPOSITORY_ROOT / CORPUS_PATH).read_bytes()
    registry = build_price_registry()
    contract = json.loads(corpus_bytes)["cases"][0]["expected"]["registry_contract"]

    assert manifest["revision"] == 5
    assert manifest["status"] == "blocked"
    assert manifest["last_accepted_rung"] == "E1"
    assert manifest["target_rung"] == "E2"
    assert manifest["corpus"]["sha256"] == hashlib.sha256(corpus_bytes).hexdigest()
    assert contract["parent_registry_snapshot_id"] == build_filing_registry().registry_snapshot_id
    assert contract["registry_snapshot_id"] == registry.registry_snapshot_id
    assert contract["registry_snapshot_sha256"] == registry.content_sha256
    assert manifest["release_activation"]["allowed"] is False
