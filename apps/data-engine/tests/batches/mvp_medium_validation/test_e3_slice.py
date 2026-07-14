from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4
from xml.etree import ElementTree

import dagster as dg
import psycopg
import pytest
from data_engine.batches.mvp_medium_validation.e3_slice import (
    D2_E3_ASSET_NAME,
    E3_CORPUS_PATH,
    D2E3Activation,
    build_d2_e3_definitions,
    load_e3_corpus,
    run_d2_e3,
)
from data_engine.config import settings
from data_engine.mvp_medium_models import MarketPricePayload
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.pq import TransactionStatus
from pydantic import ValidationError
from truealpha_contracts import RawIngestionEnvelope, RawObjectRef
from truealpha_contracts.release import ReleaseManifest

REPOSITORY_ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "governance" / "vision-issue-graph.json").is_file()
)

EXPECTED_ISSUER_COUNT = 20
EXPECTED_INSTRUMENT_COUNT = 21
EXPECTED_CELL_COUNT = 84
EXPECTED_RAW_OBJECT_COUNT = 22
E3_CORPUS_SHA256 = "aedfb76aedbac6785013bcd70c8255be171e7adf2464526357ccc801604c1c95"
TOPT_XML_SHA256 = "7e46eb6babead70230986162349bb33f27d7af2a51a095b5850340aa0a534934"
TOPT_XML_PATH = (
    REPOSITORY_ROOT
    / "apps/data-engine/tests/fixtures/mvp_medium_validation/TOPT_NPORT_000207169126012475.xml"
)
BATCH_MANIFEST_PATH = Path("governance/batches/D2-mvp-medium-validation.v1.json")
EXPECTED_ADDED_PROJECTION_COUNTS = {
    "staging.mvp_issuer_security_links": 20,
    "staging.mvp_market_prices": 20,
    "staging.mvp_security_listing_links": 20,
    "staging.mvp_universe_memberships": 21,
}
EXPECTED_E1_CASE_IDS = (
    "d0-cross-domain-regression",
    "e0-price-changed-vintage",
    "jpm-dividend-lifecycle",
    "nvda-split-lifecycle",
    "plug-financial-filing-restatement",
    "qqq-membership-vintages",
    "schema-registry-provenance",
)


class MemoryRawObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fixture_bodies: dict[str, bytes] = {}
        for corpus_path in (
            REPOSITORY_ROOT / "apps/data-engine/tests/fixtures/mvp_medium_validation/corpus.v1.json",
            REPOSITORY_ROOT / E3_CORPUS_PATH,
        ):
            corpus = json.loads(corpus_path.read_bytes())
            for artifact in corpus["artifacts"]:
                path = artifact.get("path")
                sha256 = artifact.get("sha256")
                if not isinstance(path, str) or not isinstance(sha256, str):
                    continue
                body = (REPOSITORY_ROOT / path).read_bytes()
                if hashlib.sha256(body).hexdigest() == sha256:
                    self.fixture_bodies[sha256] = body

    def store(self, capture) -> RawIngestionEnvelope:
        sha256 = hashlib.sha256(capture.body).hexdigest()
        ref = RawObjectRef(
            bucket="d2-e3-fixtures",
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
        body = self.objects.get(ref.uri)
        if body is None:
            body = self.fixture_bodies[ref.sha256]
            self.objects[ref.uri] = body
        if hashlib.sha256(body).hexdigest() != ref.sha256:
            raise ValueError("raw object checksum mismatch")
        return body


@pytest.fixture
def connection():
    try:
        admin = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")

    database_name = f"truealpha_d2_e3_{uuid4().hex}"
    active = None
    try:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(database_name)))
        parameters = conninfo_to_dict(settings.database_url)
        parameters["dbname"] = database_name
        database_url = make_conninfo(**parameters)
        migrated = psycopg.connect(database_url, connect_timeout=3, autocommit=True)
        try:
            for migration in sorted((REPOSITORY_ROOT / "db/migrations").glob("*.sql")):
                migrated.execute(migration.read_text(encoding="utf-8"))
        finally:
            migrated.close()
        active = psycopg.connect(database_url, connect_timeout=3, autocommit=False)
        yield active
    finally:
        if active is not None:
            active.rollback()
            active.close()
        admin.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s",
            (database_name,),
        )
        admin.execute(sql.SQL("drop database if exists {}").format(sql.Identifier(database_name)))
        admin.close()


def _assert_decimal_values(payload: object) -> None:
    if isinstance(payload, Decimal):
        assert payload.is_finite()
        return
    if isinstance(payload, dict):
        for value in payload.values():
            _assert_decimal_values(value)
        return
    if isinstance(payload, (list, tuple)):
        for value in payload:
            _assert_decimal_values(value)


def _mutated_repository(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
) -> Path:
    source_fixture_dir = REPOSITORY_ROOT / E3_CORPUS_PATH.parent
    target_fixture_dir = tmp_path / E3_CORPUS_PATH.parent
    shutil.copytree(source_fixture_dir, target_fixture_dir)
    corpus_path = tmp_path / E3_CORPUS_PATH
    payload = json.loads(corpus_path.read_bytes())
    mutation(payload)
    corpus_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    batch_manifest_path = tmp_path / BATCH_MANIFEST_PATH
    batch_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    batch_manifest = json.loads((REPOSITORY_ROOT / BATCH_MANIFEST_PATH).read_bytes())
    batch_manifest["terminal_corpus"]["sha256"] = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    batch_manifest_path.write_text(json.dumps(batch_manifest, indent=2) + "\n", encoding="utf-8")

    references = (
        payload["producer_handoff"]["path"],
        payload["parent_corpus"]["path"],
        *(artifact["path"] for artifact in payload["artifacts"]),
    )
    for relative_path in references:
        target = tmp_path / relative_path
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPOSITORY_ROOT / relative_path, target)
    return tmp_path


def test_e3_empty_database_run_is_complete_stable_and_idempotent(connection) -> None:
    store = MemoryRawObjectStore()
    definitions = build_d2_e3_definitions(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=store,
        activation=D2E3Activation(environment="ci"),
    )
    dg.Definitions.validate_loadable(definitions)

    first_result = definitions.get_implicit_global_asset_job_def().execute_in_process()
    repeated_result = definitions.get_implicit_global_asset_job_def().execute_in_process()
    first = first_result.output_for_node(D2_E3_ASSET_NAME)
    repeated = repeated_result.output_for_node(D2_E3_ASSET_NAME)

    assert first_result.success and repeated_result.success
    assert first == repeated
    assert first.stable_handoff is True
    assert first.handoff_id == f"mvp-topt-validation-handoff:{first.content_sha256}"
    assert first.corpus_sha256 == E3_CORPUS_SHA256
    assert first.allowed_environments == ("ci", "local")
    assert len(first.raw_object_sha256s) == EXPECTED_RAW_OBJECT_COUNT
    assert len(first.raw_object_sha256s) == len(set(first.raw_object_sha256s))
    assert all(len(value) == 64 for value in first.raw_object_sha256s)
    assert TOPT_XML_SHA256 in first.raw_object_sha256s
    assert len(first.normalized_record_ids) == len(set(first.normalized_record_ids))

    row_evidence = first.row_evidence
    assert row_evidence.required_cell_count == EXPECTED_CELL_COUNT
    assert row_evidence.observed_cell_count == EXPECTED_CELL_COUNT
    assert len(row_evidence.issuer_ids) == EXPECTED_ISSUER_COUNT
    assert len(set(row_evidence.issuer_ids)) == EXPECTED_ISSUER_COUNT
    assert len(row_evidence.instrument_ids) == EXPECTED_INSTRUMENT_COUNT
    assert len(set(row_evidence.instrument_ids)) == EXPECTED_INSTRUMENT_COUNT
    assert row_evidence.domain_counts == {
        "semantic.issuer-security-link": 21,
        "semantic.market-price": 21,
        "semantic.security-listing-link": 21,
        "semantic.universe-membership": 21,
    }
    assert row_evidence.added_projection_counts == EXPECTED_ADDED_PROJECTION_COUNTS
    assert row_evidence.action_window_cell_count == EXPECTED_INSTRUMENT_COUNT
    assert len(row_evidence.action_window_coverage) == EXPECTED_INSTRUMENT_COUNT
    assert {cell.instrument_id for cell in row_evidence.action_window_coverage} == set(
        row_evidence.instrument_ids
    )
    coverage = {cell.ticker: cell for cell in row_evidence.action_window_coverage}
    assert coverage["MU"].status == "captured_event_observed"
    assert coverage["MU"].response_event_status == "observed"
    assert coverage["MU"].observed_event_count == 1
    assert coverage["MU"].dividend_amounts == (Decimal("0.15"),)
    assert coverage["MU"].split_ratios == ()
    empty_windows = [cell for ticker, cell in coverage.items() if ticker != "MU"]
    assert len(empty_windows) == 20
    assert all(cell.status == "captured_empty" for cell in empty_windows)
    assert all(cell.response_event_status == "empty" for cell in empty_windows)
    assert all(cell.observed_event_count == 0 for cell in empty_windows)
    assert all(cell.dividend_amounts == () and cell.split_ratios == () for cell in empty_windows)

    assert row_evidence.cutoff_domain_counts == {
        "before_nport": {
            "semantic.issuer-security-link": 1,
            "semantic.market-price": 0,
            "semantic.security-listing-link": 1,
            "semantic.universe-membership": 0,
        },
        "before_prices": {
            "semantic.issuer-security-link": 21,
            "semantic.market-price": 1,
            "semantic.security-listing-link": 21,
            "semantic.universe-membership": 21,
        },
        "terminal": {
            "semantic.issuer-security-link": 21,
            "semantic.market-price": 21,
            "semantic.security-listing-link": 21,
            "semantic.universe-membership": 21,
        },
    }
    cutoff_as_of = row_evidence.cutoff_as_of
    assert tuple(cutoff_as_of) == ("before_nport", "before_prices", "terminal")
    assert cutoff_as_of["before_nport"] < cutoff_as_of["before_prices"] < cutoff_as_of["terminal"]
    assert all(value.tzinfo is not None for value in cutoff_as_of.values())

    corpus = load_e3_corpus(REPOSITORY_ROOT)
    assert corpus.corpus_sha256 == E3_CORPUS_SHA256
    instruments = {instrument.ticker: instrument for instrument in corpus.instruments}
    assert len(instruments) == EXPECTED_INSTRUMENT_COUNT
    assert instruments["GOOG"].listing_id != instruments["GOOGL"].listing_id
    assert {instruments["GOOG"].listing_id, instruments["GOOGL"].listing_id} <= set(
        row_evidence.instrument_ids
    )

    snapshot = first.snapshot
    assert len(snapshot.selections) == EXPECTED_CELL_COUNT
    assert len(snapshot.normalized_records) == EXPECTED_CELL_COUNT
    assert all(selection.normalized_record_ids for selection in snapshot.selections)
    semantic_counts = Counter(record.draft.semantic_type_id for record in snapshot.normalized_records)
    assert semantic_counts == row_evidence.domain_counts
    assert "semantic.corporate-action" not in semantic_counts
    for record in snapshot.normalized_records:
        assert isinstance(record.confidence, Decimal)
        assert record.confidence.is_finite()
        assert Decimal("0") <= record.confidence <= Decimal("1")
        assert record.raw_object_id.startswith("raw-object:")
        assert len(record.raw_object_sha256) == 64
        assert record.draft.valid_from <= record.draft.valid_to
        assert record.draft.knowable_at.tzinfo is not None
        assert record.draft.produced_at.tzinfo is not None
        assert record.recorded_at.tzinfo is not None
        assert record.draft.knowable_at <= record.draft.produced_at <= record.recorded_at
    price_records = [
        record
        for record in snapshot.normalized_records
        if record.draft.semantic_type_id == "semantic.market-price"
    ]
    assert len(price_records) == EXPECTED_INSTRUMENT_COUNT
    assert len(first.terminal_price_payloads) == EXPECTED_INSTRUMENT_COUNT
    for price in first.terminal_price_payloads:
        assert isinstance(price, MarketPricePayload)
        assert all(
            isinstance(value, Decimal) and value.is_finite()
            for value in (price.open, price.high, price.low, price.close)
        )
        _assert_decimal_values(price.model_dump(mode="python"))


def test_e3_direct_runner_matches_dagster_and_retains_e2_evidence(connection) -> None:
    store = MemoryRawObjectStore()
    direct = run_d2_e3(
        REPOSITORY_ROOT,
        connection,
        store,
        environment="local",
    )
    definitions = build_d2_e3_definitions(
        repository_root=REPOSITORY_ROOT,
        connection=connection,
        raw_store=store,
        activation=D2E3Activation(environment="local"),
    )
    materialized = definitions.get_implicit_global_asset_job_def().execute_in_process()
    through_dagster = materialized.output_for_node(D2_E3_ASSET_NAME)

    assert materialized.success
    assert direct == through_dagster
    assert direct.row_evidence.fixture_snapshot_id == direct.row_evidence.postgres_snapshot_id
    assert direct.row_evidence.fixture_snapshot_sha256 == direct.row_evidence.postgres_snapshot_sha256
    assert direct.snapshot.snapshot_id == direct.row_evidence.postgres_snapshot_id
    assert direct.e2_handoff_id.startswith("mvp-medium-validation-handoff:")
    assert direct.e2_handoff_sha256 == direct.e2_handoff_id.rsplit(":", 1)[-1]
    assert direct.retained_e2_projection_counts == {
        "staging.filing_documents": 2,
        "staging.mvp_corporate_actions": 2,
        "staging.mvp_financial_facts": 2,
        "staging.mvp_issuer_security_links": 1,
        "staging.mvp_market_prices": 2,
        "staging.mvp_security_listing_links": 1,
        "staging.mvp_universe_memberships": 203,
    }
    assert direct.retained_e2_event_bundle_id.startswith("mvp-medium-events:")
    assert len(direct.retained_e2_action_record_ids) == 2
    assert len(set(direct.retained_e2_action_record_ids)) == 2
    assert all(record_id.startswith("normalized-record:") for record_id in direct.retained_e2_action_record_ids)
    assert direct.retained_e1_case_ids == EXPECTED_E1_CASE_IDS
    assert len(direct.changed_vintage_record_ids) == 2
    assert len(set(direct.changed_vintage_record_ids)) == 2
    assert direct.prior_vintage_record_ids == (direct.changed_vintage_record_ids[0],)
    assert direct.terminal_nvda_price_record_id == direct.changed_vintage_record_ids[1]
    assert direct.append_only_controls == {"delete_rejected": True, "update_rejected": True}
    assert direct.retry_safe is True
    assert all(
        record.draft.semantic_type_id != "semantic.corporate-action"
        for record in direct.snapshot.normalized_records
    )


def test_e3_rejects_staging_release_and_wrong_e2_handoff(connection) -> None:
    with pytest.raises(ValidationError, match="environment"):
        D2E3Activation.model_validate({"environment": "staging"})
    with pytest.raises(ValidationError, match="E2|handoff"):
        D2E3Activation(
            environment="ci",
            expected_e2_handoff_id="mvp-medium-validation-handoff:" + "0" * 64,
            expected_e2_handoff_sha256="0" * 64,
        )
    with pytest.raises(ValueError, match="Local/CI|activation|release"):
        build_d2_e3_definitions(
            repository_root=REPOSITORY_ROOT,
            connection=connection,
            raw_store=MemoryRawObjectStore(),
            activation=cast(ReleaseManifest, object()),
        )


def test_e3_raw_nport_denominator_matches_candidate_and_excludes_non_equities() -> None:
    body = TOPT_XML_PATH.read_bytes()
    assert hashlib.sha256(body).hexdigest() == TOPT_XML_SHA256

    root = ElementTree.fromstring(body)
    namespace = {"nport": "http://www.sec.gov/edgar/nport"}
    holdings = root.findall(".//nport:invstOrSec", namespace)
    equity_holdings = [
        holding
        for holding in holdings
        if holding.findtext("nport:assetCat", namespaces=namespace) == "EC"
    ]

    assert len(holdings) == 23
    assert len(equity_holdings) == EXPECTED_INSTRUMENT_COUNT
    assert len(
        {
            holding.findtext("nport:lei", namespaces=namespace)
            for holding in equity_holdings
        }
    ) == EXPECTED_ISSUER_COUNT
    assert {
        holding.findtext("nport:assetCat", namespaces=namespace)
        for holding in holdings
        if holding not in equity_holdings
    } == {"DE", "STIV"}
    assert all(
        holding.findtext("nport:assetCat", namespaces=namespace) == "EC"
        for holding in equity_holdings
    )

    corpus = load_e3_corpus(REPOSITORY_ROOT)
    source = corpus.payload["universe"]["source"]
    membership = corpus.payload["row_completeness_demand"]["domains"]["universe_membership"]
    assert corpus.nport_accepted_at.isoformat() == "2026-05-28T10:14:00+00:00"
    assert source["primary_document_sha256"] == TOPT_XML_SHA256
    assert source["raw_holding_count"] == 23
    assert source["included_holding_count"] == EXPECTED_INSTRUMENT_COUNT
    assert source["excluded_holding_counts"] == {"DE": 1, "STIV": 1}
    assert membership["included_unique_lei_count"] == EXPECTED_ISSUER_COUNT
    cross_check = membership["candidate_cross_check"]
    assert cross_check["expected_instrument_count"] == EXPECTED_INSTRUMENT_COUNT
    assert cross_check["expected_issuer_count"] == EXPECTED_ISSUER_COUNT


@pytest.mark.parametrize(
    ("mutation", "error_pattern"),
    [
        (
            lambda payload: payload["row_completeness_demand"]["required_cusips"].pop(),
            "terminal row demand|missing",
        ),
        (
            lambda payload: payload["row_completeness_demand"]["required_cusips"].append(
                payload["row_completeness_demand"]["required_cusips"][0]
            ),
            "terminal row demand|duplicate",
        ),
        (
            lambda payload: payload["universe"]["instruments"].pop(),
            "instrument denominator shrank",
        ),
        (
            lambda payload: payload["universe"]["instruments"][0].update(
                {"issuer_lei": payload["universe"]["instruments"][1]["issuer_lei"]}
            ),
            "instrument denominator|subject",
        ),
        (lambda payload: payload.update({"schema_version": 2}), "corpus schema"),
        (
            lambda payload: next(
                artifact
                for artifact in payload["artifacts"]
                if artifact["artifact_id"] == "yahoo-price-aapl-20260331"
            ).update({"sha256": "0" * 64}),
            "artifact bytes|SHA-256|checksum",
        ),
    ],
    ids=[
        "missing-required-cell",
        "duplicate-required-cell",
        "denominator-shrink",
        "wrong-subject",
        "schema-drift",
        "checksum-mismatch",
    ],
)
def test_e3_corpus_mutations_fail_closed(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    error_pattern: str,
) -> None:
    repository_root = _mutated_repository(tmp_path, mutation)
    with pytest.raises((ValidationError, ValueError), match=error_pattern):
        load_e3_corpus(repository_root)


@pytest.mark.parametrize(
    ("location", "artifact_id", "error_pattern"),
    [
        ("result", "yahoo-price-aapl-20260331", "Yahoo result schema drifted"),
        ("meta", "yahoo-price-aapl-20260331", "Yahoo meta schema drifted"),
        ("quote", "yahoo-price-aapl-20260331", "Yahoo quote schema drifted"),
        ("dividend", "yahoo-price-mu-20260331", "Yahoo dividend schema drifted"),
    ],
    ids=["result", "meta", "quote", "dividend-event"],
)
def test_e3_yahoo_unknown_fields_fail_after_all_hashes_are_rebound(
    tmp_path: Path,
    location: str,
    artifact_id: str,
    error_pattern: str,
) -> None:
    def add_unknown_field(payload: dict[str, Any]) -> None:
        artifact = next(item for item in payload["artifacts"] if item["artifact_id"] == artifact_id)
        artifact_path = tmp_path / artifact["path"]
        source_payload = json.loads(artifact_path.read_bytes())
        result = cast(dict[str, Any], source_payload["chart"]["result"][0])
        if location == "result":
            target = result
        elif location == "meta":
            target = cast(dict[str, Any], result["meta"])
        elif location == "quote":
            target = cast(dict[str, Any], result["indicators"]["quote"][0])
        else:
            dividends = cast(dict[str, dict[str, Any]], result["events"]["dividends"])
            target = next(iter(dividends.values()))
        target["unexpected"] = "schema-drift"
        body = (json.dumps(source_payload, separators=(",", ":")) + "\n").encode()
        artifact_path.write_bytes(body)
        artifact["sha256"] = hashlib.sha256(body).hexdigest()
        artifact["byte_length"] = len(body)

    repository_root = _mutated_repository(tmp_path, add_unknown_field)
    rebound_corpus_path = repository_root / E3_CORPUS_PATH
    rebound_corpus_body = rebound_corpus_path.read_bytes()
    rebound_corpus = json.loads(rebound_corpus_body)
    rebound_artifact = next(
        item for item in rebound_corpus["artifacts"] if item["artifact_id"] == artifact_id
    )
    rebound_artifact_body = (repository_root / rebound_artifact["path"]).read_bytes()
    rebound_manifest = json.loads((repository_root / BATCH_MANIFEST_PATH).read_bytes())
    assert rebound_artifact["sha256"] == hashlib.sha256(rebound_artifact_body).hexdigest()
    assert rebound_artifact["byte_length"] == len(rebound_artifact_body)
    assert rebound_manifest["terminal_corpus"]["sha256"] == hashlib.sha256(
        rebound_corpus_body
    ).hexdigest()

    with pytest.raises(ValueError, match=error_pattern) as error:
        load_e3_corpus(repository_root)
    assert "checksum" not in str(error.value).lower()


def test_e3_yahoo_source_schema_drift_fails_after_checksum_rebinding(tmp_path: Path) -> None:
    def add_unsupported_event(payload: dict[str, Any]) -> None:
        artifact = next(
            item
            for item in payload["artifacts"]
            if item["artifact_id"] == "yahoo-price-aapl-20260331"
        )
        artifact_path = tmp_path / artifact["path"]
        source_payload = json.loads(artifact_path.read_bytes())
        result = source_payload["chart"]["result"][0]
        result.setdefault("events", {})["capitalGains"] = {}
        body = (json.dumps(source_payload, separators=(",", ":")) + "\n").encode()
        artifact_path.write_bytes(body)
        artifact["sha256"] = hashlib.sha256(body).hexdigest()
        artifact["byte_length"] = len(body)

    repository_root = _mutated_repository(tmp_path, add_unsupported_event)
    with pytest.raises(ValueError, match="unsupported Yahoo event type"):
        load_e3_corpus(repository_root)


def test_e3_failure_rolls_back_and_retry_does_not_duplicate_outputs(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("select count(*) from staging.normalized_records")
        before_count = cursor.fetchone()[0]
    connection.rollback()

    store = MemoryRawObjectStore()
    with pytest.raises(RuntimeError, match="inject|fail"):
        run_d2_e3(
            REPOSITORY_ROOT,
            connection,
            store,
            environment="ci",
            fail_after_normalized_records=40,
        )

    assert connection.info.transaction_status is not TransactionStatus.INERROR
    with connection.cursor() as cursor:
        cursor.execute("select count(*) from staging.normalized_records")
        assert cursor.fetchone()[0] == before_count
    connection.rollback()

    retried = run_d2_e3(REPOSITORY_ROOT, connection, store, environment="ci")
    with connection.cursor() as cursor:
        cursor.execute("select count(*) from staging.normalized_records")
        after_retry_count = cursor.fetchone()[0]
    connection.rollback()
    repeated = run_d2_e3(REPOSITORY_ROOT, connection, store, environment="ci")
    with connection.cursor() as cursor:
        cursor.execute("select count(*) from staging.normalized_records")
        after_repeat_count = cursor.fetchone()[0]

    assert retried == repeated
    assert retried.retry_safe is True
    assert after_retry_count == after_repeat_count
    assert len(retried.normalized_record_ids) == len(set(retried.normalized_record_ids))
