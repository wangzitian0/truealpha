import hashlib
import os

import dagster as dg
import psycopg
import pytest
from data_engine.config import settings
from data_engine.financial_facts_assets import (
    FINANCIAL_FACTS_ASSET_NAME,
    SAMPLE_ISSUERS,
    build_financial_facts_definitions,
)
from truealpha_contracts import RawIngestionEnvelope, RawObjectRef


class MemoryRawObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def store(self, capture) -> RawIngestionEnvelope:
        content_sha256 = hashlib.sha256(capture.body).hexdigest()
        ref = RawObjectRef(
            bucket="financial-facts-fixtures",
            key=content_sha256,
            sha256=content_sha256,
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


def test_dagster_composition_is_explicit_local_ci_only(connection) -> None:
    import data_engine.financial_facts_assets as financial_facts_assets

    definitions = build_financial_facts_definitions(connection=connection, raw_store=MemoryRawObjectStore())
    dg.Definitions.validate_loadable(definitions)
    assert not definitions.schedules
    assert not definitions.sensors
    assert not hasattr(financial_facts_assets, "defs")


def test_materialization_lands_every_sample_issuer(connection) -> None:
    definitions = build_financial_facts_definitions(connection=connection, raw_store=MemoryRawObjectStore())

    result = definitions.get_implicit_global_asset_job_def().execute_in_process()

    assert result.success
    written = result.output_for_node(FINANCIAL_FACTS_ASSET_NAME)
    assert set(written) == set(SAMPLE_ISSUERS)
    for metrics in written.values():
        assert "total_assets" in metrics

    row_count = connection.execute(
        "select count(*) from staging.financial_facts where unified_id = any(%s)",
        ([f"issuer.{ticker}" for ticker in written],),
    ).fetchone()[0]
    assert row_count == sum(len(metrics) for metrics in written.values())


def test_materialization_metadata_reports_counts(connection) -> None:
    definitions = build_financial_facts_definitions(connection=connection, raw_store=MemoryRawObjectStore())
    result = definitions.get_implicit_global_asset_job_def().execute_in_process()

    materialization = next(
        event.event_specific_data.materialization
        for event in result.all_events
        if event.is_step_materialization and event.step_key == FINANCIAL_FACTS_ASSET_NAME
    )
    assert materialization.metadata["issuer_count"].value == len(SAMPLE_ISSUERS)
    assert materialization.metadata["metric_count"].value > 0
