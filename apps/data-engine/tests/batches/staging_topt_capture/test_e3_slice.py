from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

import httpx
import psycopg
import pytest
from data_engine.batches.mvp_medium_validation.e3_slice import load_topt_denominator, load_topt_market_fixture
from data_engine.batches.staging_topt_capture.e0_slice import SOURCE_ID
from data_engine.batches.staging_topt_capture.e1_slice import D3E1InteractionError
from data_engine.batches.staging_topt_capture.e3_slice import (
    D3_E3_SOURCE_COVERAGE_ENTRY_ID,
    build_e3_yahoo_response,
    run_d3_e3,
)
from data_engine.config import settings
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from truealpha_contracts import RawIngestionEnvelope, RawObjectRef, evaluate_capture_manifest
from truealpha_contracts.data_quality import DataDomain

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
            bucket="d3-e3-tests",
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
    database_name = f"truealpha_d3_e3_{os.getpid()}_{uuid.uuid4().hex[:8]}"
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


def _fixture():
    denominator = load_topt_denominator(REPOSITORY_ROOT)
    return load_topt_market_fixture(REPOSITORY_ROOT, denominator)


def _mock_client(*, missing_symbol: str | None = None) -> tuple[httpx.Client, Counter[str]]:
    rows = {row.vendor_symbol: row for row in _fixture().rows}
    calls: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        symbol = unquote(request.url.path.rsplit("/", 1)[-1])
        calls[symbol] += 1
        if symbol == missing_symbol:
            return httpx.Response(404, headers={"content-type": "application/json"})
        row = rows[symbol]
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=build_e3_yahoo_response(row, changed=calls[symbol] % 2 == 0),
        )

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def _d3_raw_count(connection) -> int:
    row = connection.execute(
        "select count(*) from raw.fetches where metadata ->> 'source_id' = %s",
        (SOURCE_ID,),
    ).fetchone()
    assert row is not None
    return row[0]


def test_e3_persists_two_full_denominator_vintages_idempotently(connection) -> None:
    client, calls = _mock_client()
    store = MemoryRawObjectStore()
    with client:
        first = run_d3_e3(REPOSITORY_ROOT, connection, store, client, environment="ci")
        repeated = run_d3_e3(REPOSITORY_ROOT, connection, store, client, environment="ci")

    assert repeated == first
    assert calls == Counter({row.vendor_symbol: 4 for row in _fixture().rows})
    assert len(first.interactions) == 42
    assert len({item.result.normalized_bar.security_id for item in first.interactions}) == 21
    assert len({item.result.normalized_bar.issuer_id for item in first.interactions}) == 20
    assert len(first.capture_scope.requirements) == 4
    assert all(len(manifest.cells) == 84 for manifest in first.capture_manifests)
    assert all(evaluation.ready for evaluation in first.capture_evaluations)
    assert _d3_raw_count(connection) == 42
    assert len(
        {
            item.normalized_record_id
            for item in first.interactions
            if item.result.normalized_bar.symbol in {"GOOG", "GOOGL"}
        }
    ) == 4
    assert connection.execute(
        "select count(*) from staging.normalized_records where normalized_record_id = any(%s)",
        ([item.normalized_record_id for item in first.interactions],),
    ).fetchone() == (42,)

    source_coverage = {
        key: (D3_E3_SOURCE_COVERAGE_ENTRY_ID,) if key[3] is DataDomain.MARKET_PRICES else values
        for key, values in first.accepted_d2_evidence.capture_plan.source_coverage_mapping().items()
    }
    manifest = first.capture_manifests[0]
    market_index = next(index for index, cell in enumerate(manifest.cells) if cell.domain is DataDomain.MARKET_PRICES)
    market_cell = manifest.cells[market_index]
    for update in ({"raw_id": None}, {"normalized_id": None}, {"confidence": None}, {"lineage_sha256": None}):
        broken_evidence = market_cell.evidence[0].model_copy(update=update)
        broken_cell = market_cell.model_copy(update={"evidence": (broken_evidence,)})
        broken_cells = list(manifest.cells)
        broken_cells[market_index] = broken_cell
        broken_manifest = manifest.model_copy(update={"cells": tuple(broken_cells)})
        evaluation = evaluate_capture_manifest(
            first.capture_scope,
            broken_manifest,
            applicability_catalog_id=first.capture_scope.applicability_catalog_id,
            applicability_catalog_sha256=first.capture_scope.applicability_catalog_sha256,
            applicability=first.accepted_d2_evidence.capture_plan.applicability_mapping(),
            source_coverage=source_coverage,
            evaluated_at=manifest.created_at,
        )
        assert not evaluation.ready


def test_e3_missing_symbol_rolls_back_all_database_rows(connection) -> None:
    client, calls = _mock_client(missing_symbol="XOM")
    with client, pytest.raises(D3E1InteractionError, match="non-retryable HTTP status"):
        run_d3_e3(REPOSITORY_ROOT, connection, MemoryRawObjectStore(), client, environment="ci")

    assert calls["XOM"] == 1
    assert connection.execute("select count(*) from raw.fetches").fetchone() == (0,)
    assert connection.execute("select count(*) from staging.normalized_records").fetchone() == (0,)
    assert connection.execute("select count(*) from staging.contract_objects").fetchone() == (0,)
