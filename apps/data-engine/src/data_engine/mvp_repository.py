"""Append-only Postgres persistence for the D1 filing-document slice."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from truealpha_contracts.execution import NormalizedRecordRef
from truealpha_contracts.universe import SubjectRef

from data_engine.mvp_models import FilingDocumentPayload


class PostgresFilingDocumentRepository:
    def __init__(self, connection: Connection[Any]) -> None:
        self.connection = connection

    def put(self, record: NormalizedRecordRef, payload: FilingDocumentPayload, *, raw_ref: str) -> bool:
        record_json = record.model_dump(mode="json")
        payload_json = payload.model_dump(mode="json")
        with self.connection.transaction():
            self._validate_supersedes(record)
            inserted = self.connection.execute(
                """
                insert into staging.normalized_records (
                    normalized_record_id, content_sha256, semantic_type_id,
                    semantic_type_version, subject_kind, subject_id, valid_time,
                    transaction_time, recorded_at, confidence, document_id,
                    raw_object_id, raw_object_sha256, raw_ref,
                    source_registry_entry_id, source_registry_entry_sha256,
                    mapping_version, mapping_implementation_sha256,
                    payload_model_key, payload_schema_sha256, payload_sha256,
                    payload, record_ref, is_restatement, supersedes_record_id
                ) values (
                    %s, %s, %s, %s, %s, %s, daterange(%s, %s, '[]'),
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                on conflict (normalized_record_id) do nothing
                returning normalized_record_id
                """,
                (
                    record.normalized_record_id,
                    record.content_sha256,
                    record.draft.semantic_type_id,
                    record.draft.semantic_type_version,
                    record.draft.subject.kind.value,
                    record.draft.subject.id,
                    record.draft.valid_from,
                    record.draft.valid_to,
                    record.draft.knowable_at,
                    record.recorded_at,
                    record.confidence,
                    record.document_id,
                    record.raw_object_id,
                    record.raw_object_sha256,
                    raw_ref,
                    record.source_registry_entry_id,
                    record.source_registry_entry_sha256,
                    record.mapping_version,
                    record.mapping_implementation_sha256,
                    record.draft.payload_model_key,
                    record.draft.payload_schema_sha256,
                    record.draft.payload_sha256,
                    Jsonb(payload_json),
                    Jsonb(record_json),
                    record.is_restatement,
                    record.supersedes_record_id,
                ),
            ).fetchone()
            if inserted is None:
                self._validate_existing(record, payload, raw_ref=raw_ref)
                return False
            self.connection.execute(
                """
                insert into staging.filing_documents (
                    normalized_record_id, document_id, issuer_id, accession,
                    form, filing_date, report_period, content_sha256,
                    content_type, valid_time, transaction_time, recorded_at,
                    confidence, raw_ref
                ) values (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    daterange(%s, %s, '[]'), %s, %s, %s, %s
                )
                """,
                (
                    record.normalized_record_id,
                    record.document_id,
                    record.draft.subject.id,
                    payload.accession,
                    payload.form,
                    payload.filing_date,
                    payload.report_period,
                    payload.content_sha256,
                    payload.content_type,
                    record.draft.valid_from,
                    record.draft.valid_to,
                    record.draft.knowable_at,
                    record.recorded_at,
                    record.confidence,
                    raw_ref,
                ),
            )
        return True

    def select_pit(
        self,
        *,
        subject: SubjectRef,
        semantic_type_id: str,
        semantic_type_version: str,
        source_registry_entry_id: str,
        as_of: datetime,
        valid_on: date,
    ) -> tuple[NormalizedRecordRef, ...]:
        rows = self.connection.execute(
            """
            select candidate.record_ref
            from staging.normalized_records candidate
            where candidate.subject_kind = %s
              and candidate.subject_id = %s
              and candidate.semantic_type_id = %s
              and candidate.semantic_type_version = %s
              and candidate.source_registry_entry_id = %s
              and candidate.transaction_time <= %s
              and candidate.valid_time @> %s::date
              and not exists (
                  select 1
                  from staging.normalized_records replacement
                  where replacement.supersedes_record_id = candidate.normalized_record_id
                    and replacement.semantic_type_id = candidate.semantic_type_id
                    and replacement.semantic_type_version = candidate.semantic_type_version
                    and replacement.source_registry_entry_id = candidate.source_registry_entry_id
                    and replacement.transaction_time <= %s
              )
            order by candidate.transaction_time desc, candidate.normalized_record_id desc
            """,
            (
                subject.kind.value,
                subject.id,
                semantic_type_id,
                semantic_type_version,
                source_registry_entry_id,
                as_of,
                valid_on,
                as_of,
            ),
        ).fetchall()
        return tuple(NormalizedRecordRef.model_validate(row[0]) for row in rows)

    def all_records(self, *, subject: SubjectRef) -> tuple[NormalizedRecordRef, ...]:
        rows = self.connection.execute(
            """
            select record_ref
            from staging.normalized_records
            where subject_kind = %s and subject_id = %s
            order by normalized_record_id
            """,
            (subject.kind.value, subject.id),
        ).fetchall()
        return tuple(NormalizedRecordRef.model_validate(row[0]) for row in rows)

    def payload_for(self, normalized_record_id: str) -> FilingDocumentPayload:
        row = self.connection.execute(
            "select payload from staging.normalized_records where normalized_record_id = %s",
            (normalized_record_id,),
        ).fetchone()
        if row is None:
            raise LookupError(normalized_record_id)
        return FilingDocumentPayload.model_validate(row[0])

    def _validate_supersedes(self, record: NormalizedRecordRef) -> None:
        if record.supersedes_record_id is None:
            return
        predecessor = self.connection.execute(
            """
            select semantic_type_id, semantic_type_version, subject_kind, subject_id,
                   source_registry_entry_id, source_registry_entry_sha256,
                   valid_time = daterange(%s, %s, '[]'), transaction_time
            from staging.normalized_records
            where normalized_record_id = %s
            """,
            (record.draft.valid_from, record.draft.valid_to, record.supersedes_record_id),
        ).fetchone()
        expected = (
            record.draft.semantic_type_id,
            record.draft.semantic_type_version,
            record.draft.subject.kind.value,
            record.draft.subject.id,
            record.source_registry_entry_id,
            record.source_registry_entry_sha256,
        )
        if predecessor is None or predecessor[:6] != expected:
            raise ValueError("superseded record must exist in the same registry-bound semantic coordinate")
        if predecessor[6] is not True:
            raise ValueError("superseded record must retain the same valid period")
        if record.draft.knowable_at <= predecessor[7]:
            raise ValueError("superseding record must have a strictly later transaction time")
        competing = self.connection.execute(
            """
            select normalized_record_id
            from staging.normalized_records
            where supersedes_record_id = %s and normalized_record_id <> %s
            limit 1
            """,
            (record.supersedes_record_id, record.normalized_record_id),
        ).fetchone()
        if competing is not None:
            raise ValueError("a normalized record cannot have multiple successors")

    def _validate_existing(
        self,
        record: NormalizedRecordRef,
        payload: FilingDocumentPayload,
        *,
        raw_ref: str,
    ) -> None:
        row = self.connection.execute(
            """
            select record_ref, payload, raw_ref
            from staging.normalized_records
            where normalized_record_id = %s
            """,
            (record.normalized_record_id,),
        ).fetchone()
        expected = (record.model_dump(mode="json"), payload.model_dump(mode="json"), raw_ref)
        if row is None or tuple(row) != expected:
            raise ValueError("normalized record ID is already bound to different content")


__all__ = ["PostgresFilingDocumentRepository"]
