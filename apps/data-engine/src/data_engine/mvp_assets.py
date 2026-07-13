"""Explicit Dagster composition for the D1 E0 filing evidence."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import dagster as dg
from dagster import AssetExecutionContext
from psycopg import Connection
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts import RawObjectStore
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.release import ReleaseManifest

from data_engine.mvp_pipeline import run_filing_pipeline
from data_engine.mvp_repository import PostgresFilingDocumentRepository
from data_engine.mvp_snapshot import build_filing_snapshot
from data_engine.mvp_sources import FROZEN_CORPUS_SHA256

D1_E0_ASSET_NAME = "mvp_filing_document_e0_evidence"


class D1E0Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(default="", pattern=r"^(?:|d1-e0-evidence:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_ids: tuple[str, ...] = Field(min_length=2, max_length=2)
    fixture_evaluation_id: str
    postgres_evaluation_id: str
    fixture_snapshot_id: str = Field(pattern=r"^snapshot:[0-9a-f]{64}$")
    postgres_snapshot_id: str = Field(pattern=r"^snapshot:[0-9a-f]{64}$")
    fixture_runner_selection_id: str = Field(pattern=r"^runner-selection:[0-9a-f]{64}$")
    postgres_runner_selection_id: str = Field(pattern=r"^runner-selection:[0-9a-f]{64}$")
    pre_amendment_record_id: str = Field(pattern=r"^normalized-record:[0-9a-f]{64}$")
    at_amendment_record_id: str = Field(pattern=r"^normalized-record:[0-9a-f]{64}$")
    post_amendment_record_id: str = Field(pattern=r"^normalized-record:[0-9a-f]{64}$")
    row_counts: dict[str, int]
    stable_handoff: Literal[False] = False
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def freeze_and_identify(self) -> "D1E0Evidence":
        records = tuple(sorted(set(self.record_ids)))
        if len(records) != 2:
            raise ValueError("D1 E0 requires exactly two normalized filing records")
        if self.fixture_snapshot_id != self.postgres_snapshot_id:
            raise ValueError("fixture and Postgres snapshot identities differ")
        if self.fixture_runner_selection_id != self.postgres_runner_selection_id:
            raise ValueError("fixture and Postgres runner selections differ")
        if self.pre_amendment_record_id == self.at_amendment_record_id:
            raise ValueError("pre-amendment PIT selection did not retain the original")
        if self.at_amendment_record_id != self.post_amendment_record_id:
            raise ValueError("amendment publication boundary is unstable")
        expected_counts = {"filing_documents": 2, "normalized_records": 2, "raw_fetches": 2}
        if self.row_counts != expected_counts:
            raise ValueError("D1 E0 persistent row counts are incomplete or duplicated")
        object.__setattr__(self, "record_ids", records)
        object.__setattr__(self, "row_counts", dict(sorted(self.row_counts.items())))
        payload = self.model_dump(mode="json", exclude={"evidence_id", "content_sha256"})
        expected_hash = canonical_sha256(payload)
        expected_id = f"d1-e0-evidence:{expected_hash}"
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("content_sha256 does not match D1 E0 evidence")
        if self.evidence_id and self.evidence_id != expected_id:
            raise ValueError("evidence_id does not match D1 E0 evidence")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "evidence_id", expected_id)
        return self


def run_d1_e0(
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
) -> D1E0Evidence:
    pipeline = run_filing_pipeline(
        repository_root=repository_root,
        connection=connection,
        raw_store=raw_store,
    )
    original, amended = pipeline.records
    repository = PostgresFilingDocumentRepository(connection)
    pre = repository.select_pit(
        subject=original.draft.subject,
        semantic_type_id=original.draft.semantic_type_id,
        semantic_type_version=original.draft.semantic_type_version,
        source_registry_entry_id=original.source_registry_entry_id,
        valid_on=original.draft.valid_to,
        as_of=amended.draft.knowable_at - timedelta(microseconds=1),
    )
    at = repository.select_pit(
        subject=original.draft.subject,
        semantic_type_id=original.draft.semantic_type_id,
        semantic_type_version=original.draft.semantic_type_version,
        source_registry_entry_id=original.source_registry_entry_id,
        valid_on=original.draft.valid_to,
        as_of=amended.draft.knowable_at,
    )
    post = repository.select_pit(
        subject=original.draft.subject,
        semantic_type_id=original.draft.semantic_type_id,
        semantic_type_version=original.draft.semantic_type_version,
        source_registry_entry_id=original.source_registry_entry_id,
        valid_on=original.draft.valid_to,
        as_of=amended.draft.knowable_at + timedelta(microseconds=1),
    )
    if len(pre) != 1 or len(at) != 1 or len(post) != 1:
        raise ValueError("filing PIT selection is not singular at the publication boundary")
    fixture_bundle = build_filing_snapshot(
        records=pipeline.records,
        selected_record=amended,
        registry=pipeline.registry,
        as_of=amended.draft.knowable_at,
    )
    postgres_bundle = build_filing_snapshot(
        records=at,
        selected_record=at[0],
        registry=pipeline.registry,
        as_of=amended.draft.knowable_at,
    )
    raw_count = connection.execute(
        """
        select count(*)
        from raw.fetches
        where (source_record_id, payload_sha256) in ((%s, %s), (%s, %s))
        """,
        (
            pipeline.artifacts[0].source_record_id,
            pipeline.artifacts[0].sha256,
            pipeline.artifacts[1].source_record_id,
            pipeline.artifacts[1].sha256,
        ),
    ).fetchone()
    normalized_count = connection.execute(
        "select count(*) from staging.normalized_records where normalized_record_id = any(%s)",
        ([record.normalized_record_id for record in pipeline.records],),
    ).fetchone()
    filing_count = connection.execute(
        "select count(*) from staging.filing_documents where normalized_record_id = any(%s)",
        ([record.normalized_record_id for record in pipeline.records],),
    ).fetchone()
    if raw_count is None or normalized_count is None or filing_count is None:
        raise ValueError("persistent filing row counts could not be read")
    return D1E0Evidence(
        corpus_sha256=FROZEN_CORPUS_SHA256,
        record_ids=tuple(record.normalized_record_id for record in pipeline.records),
        fixture_evaluation_id=fixture_bundle.evaluation.capture_evaluation_report_id,
        postgres_evaluation_id=postgres_bundle.evaluation.capture_evaluation_report_id,
        fixture_snapshot_id=fixture_bundle.snapshot.snapshot_id,
        postgres_snapshot_id=postgres_bundle.snapshot.snapshot_id,
        fixture_runner_selection_id=fixture_bundle.runner_selection.selection_id,
        postgres_runner_selection_id=postgres_bundle.runner_selection.selection_id,
        pre_amendment_record_id=pre[0].normalized_record_id,
        at_amendment_record_id=at[0].normalized_record_id,
        post_amendment_record_id=post[0].normalized_record_id,
        row_counts={
            "raw_fetches": raw_count[0],
            "normalized_records": normalized_count[0],
            "filing_documents": filing_count[0],
        },
        created_at=amended.recorded_at + timedelta(minutes=10),
    )


@dataclass(frozen=True)
class D1E0RunnerResource:
    repository_root: Path
    connection: Connection[Any]
    raw_store: RawObjectStore

    def run(self) -> D1E0Evidence:
        return run_d1_e0(self.repository_root, self.connection, self.raw_store)


class D1ProvisionalActivation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: Literal["D1-mvp-normalization-handoff"] = "D1-mvp-normalization-handoff"
    release_allowed: Literal[False] = False


@dg.asset(
    name=D1_E0_ASSET_NAME,
    group_name="mvp_filing_document_e0",
    required_resource_keys={"d1_e0_runner"},
    description="Execute the frozen D1 E0 filing slice without release activation.",
)
def materialize_mvp_filing_document_e0(context: AssetExecutionContext) -> dg.Output[D1E0Evidence]:
    runner = cast(D1E0RunnerResource, context.resources.d1_e0_runner)
    evidence = runner.run()
    return dg.Output(
        evidence,
        metadata={
            "evidence_id": evidence.evidence_id,
            "record_count": len(evidence.record_ids),
            "stable_handoff": evidence.stable_handoff,
        },
        data_version=dg.DataVersion(evidence.content_sha256),
    )


def build_d1_e0_definitions(
    *,
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    activation: D1ProvisionalActivation | ReleaseManifest,
) -> dg.Definitions:
    if not isinstance(activation, D1ProvisionalActivation):
        raise ValueError("the provisional D1 E0 batch is not release-activated")
    return dg.Definitions(
        assets=[materialize_mvp_filing_document_e0],
        resources={
            "d1_e0_runner": cast(
                Any,
                D1E0RunnerResource(
                    repository_root=repository_root,
                    connection=connection,
                    raw_store=raw_store,
                ),
            )
        },
    )


__all__ = [
    "D1_E0_ASSET_NAME",
    "D1E0Evidence",
    "D1E0RunnerResource",
    "D1ProvisionalActivation",
    "build_d1_e0_definitions",
    "materialize_mvp_filing_document_e0",
    "run_d1_e0",
]
