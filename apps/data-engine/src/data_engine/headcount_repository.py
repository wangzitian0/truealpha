"""Append-only Postgres persistence for H0 headcount extractions."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from truealpha_contracts.execution import (
    ExtractionInvocation,
    ExtractionTemplate,
    ModelRevisionRef,
    NormalizedRecordRef,
)
from truealpha_contracts.universe import SubjectRef

from data_engine.headcount_models import (
    EvidenceSpan,
    HeadcountExtractionBundle,
    HeadcountPayload,
    headcount_input_sha256,
)


def _all_evidence_spans(payload: HeadcountPayload) -> tuple[EvidenceSpan, ...]:
    by_id = {span.evidence_span_id: span for candidate in payload.candidates for span in candidate.evidence_spans}
    return tuple(by_id[key] for key in sorted(by_id))


class PostgresHeadcountRepository:
    def __init__(self, connection: Connection[Any]) -> None:
        self.connection = connection

    def put(self, bundle: HeadcountExtractionBundle) -> bool:
        with self.connection.transaction():
            self._put_invocation(bundle)
            inserted = self._put_normalized_record(bundle)
            if inserted:
                self._put_projection(bundle)
            else:
                self._validate_existing_result(bundle)
        return inserted

    def load(
        self,
        extraction_invocation_id: str,
        *,
        model_revision: ModelRevisionRef,
        template: ExtractionTemplate,
    ) -> HeadcountExtractionBundle:
        row = self.connection.execute(
            """
            select invocation.invocation, result.record_ref, result.payload,
                   result.raw_ref, invocation.source_document_record_id,
                   result.evidence_spans
            from staging.headcount_extraction_invocations invocation
            join staging.headcount_facts result
              on result.extraction_invocation_id = invocation.extraction_invocation_id
            where invocation.extraction_invocation_id = %s
            """,
            (extraction_invocation_id,),
        ).fetchone()
        if row is None:
            raise LookupError(extraction_invocation_id)
        invocation = ExtractionInvocation.model_validate(row[0])
        record = NormalizedRecordRef.model_validate(row[1])
        payload = HeadcountPayload.model_validate(row[2])
        stored_spans = tuple(EvidenceSpan.model_validate(value) for value in row[5])
        if stored_spans != _all_evidence_spans(payload):
            raise ValueError("stored headcount evidence spans do not match the semantic payload")
        return HeadcountExtractionBundle(
            source_document_record_id=row[4],
            raw_ref=row[3],
            model_revision=model_revision,
            template=template,
            invocation=invocation,
            record=record,
            payload=payload,
        )

    def load_for_input(
        self,
        document_record: NormalizedRecordRef,
        *,
        model_revision: ModelRevisionRef,
        template: ExtractionTemplate,
    ) -> HeadcountExtractionBundle | None:
        rows = self.connection.execute(
            """
            select extraction_invocation_id
            from staging.headcount_extraction_invocations
            where source_document_record_id = %s
              and model_revision_id = %s
              and model_revision_sha256 = %s
              and extraction_template_id = %s
              and extraction_template_sha256 = %s
              and input_sha256 = %s
            order by extraction_invocation_id
            """,
            (
                document_record.normalized_record_id,
                model_revision.model_revision_id,
                model_revision.content_sha256,
                template.extraction_template_id,
                template.content_sha256,
                headcount_input_sha256(document_record),
            ),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError("one frozen document and extraction identity produced multiple invocations")
        return self.load(rows[0][0], model_revision=model_revision, template=template)

    def select_pit(
        self,
        *,
        subject: SubjectRef,
        source_registry_entry_id: str,
        valid_on: date,
        as_of: datetime,
        model_revision: ModelRevisionRef,
        template: ExtractionTemplate,
    ) -> tuple[HeadcountExtractionBundle, ...]:
        rows = self.connection.execute(
            """
            select invocation.extraction_invocation_id
            from staging.headcount_facts fact
            join staging.normalized_records candidate
              on candidate.normalized_record_id = fact.normalized_record_id
            join staging.headcount_extraction_invocations invocation
              on invocation.extraction_invocation_id = fact.extraction_invocation_id
            where candidate.subject_kind = %s
              and candidate.subject_id = %s
              and candidate.semantic_type_id = 'semantic.employee-headcount'
              and candidate.source_registry_entry_id = %s
              and candidate.transaction_time <= %s
              and candidate.valid_time @> %s::date
              and invocation.model_revision_id = %s
              and invocation.model_revision_sha256 = %s
              and invocation.extraction_template_id = %s
              and invocation.extraction_template_sha256 = %s
              and not exists (
                  select 1
                  from staging.normalized_records replacement
                  where replacement.supersedes_record_id = candidate.normalized_record_id
                    and replacement.transaction_time <= %s
              )
            order by candidate.transaction_time desc, candidate.normalized_record_id desc
            """,
            (
                subject.kind.value,
                subject.id,
                source_registry_entry_id,
                as_of,
                valid_on,
                model_revision.model_revision_id,
                model_revision.content_sha256,
                template.extraction_template_id,
                template.content_sha256,
                as_of,
            ),
        ).fetchall()
        return tuple(self.load(row[0], model_revision=model_revision, template=template) for row in rows)

    def _put_invocation(self, bundle: HeadcountExtractionBundle) -> None:
        invocation = bundle.invocation
        inserted = self.connection.execute(
            """
            insert into staging.headcount_extraction_invocations (
                extraction_invocation_id, content_sha256,
                source_document_record_id, document_id, document_sha256, raw_ref,
                model_revision_id, model_revision_sha256,
                extraction_template_id, extraction_template_sha256,
                input_sha256, response_sha256, semantic_payload_sha256,
                started_at, completed_at, recorded_at, invocation
            ) values (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s
            )
            on conflict (extraction_invocation_id) do nothing
            returning extraction_invocation_id
            """,
            (
                invocation.extraction_invocation_id,
                invocation.content_sha256,
                bundle.source_document_record_id,
                bundle.record.document_id,
                bundle.record.raw_object_sha256,
                bundle.raw_ref,
                invocation.model_revision_id,
                invocation.model_revision_sha256,
                invocation.extraction_template_id,
                invocation.extraction_template_sha256,
                invocation.input_sha256,
                invocation.response_sha256,
                invocation.semantic_payload_sha256,
                invocation.started_at,
                invocation.completed_at,
                bundle.record.recorded_at,
                Jsonb(invocation.model_dump(mode="json")),
            ),
        ).fetchone()
        if inserted is not None:
            return
        row = self.connection.execute(
            """
            select invocation, source_document_record_id, document_id,
                   document_sha256, raw_ref, recorded_at
            from staging.headcount_extraction_invocations
            where extraction_invocation_id = %s
            """,
            (invocation.extraction_invocation_id,),
        ).fetchone()
        expected = (
            invocation.model_dump(mode="json"),
            bundle.source_document_record_id,
            bundle.record.document_id,
            bundle.record.raw_object_sha256,
            bundle.raw_ref,
            bundle.record.recorded_at,
        )
        if row is None or tuple(row) != expected:
            raise ValueError("extraction invocation ID is already bound to different content")

    def _put_normalized_record(self, bundle: HeadcountExtractionBundle) -> bool:
        record = bundle.record
        payload_json = bundle.payload.model_dump(mode="json")
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
                bundle.raw_ref,
                record.source_registry_entry_id,
                record.source_registry_entry_sha256,
                record.mapping_version,
                record.mapping_implementation_sha256,
                record.draft.payload_model_key,
                record.draft.payload_schema_sha256,
                record.draft.payload_sha256,
                Jsonb(payload_json),
                Jsonb(record.model_dump(mode="json")),
                record.is_restatement,
                record.supersedes_record_id,
            ),
        ).fetchone()
        return inserted is not None

    def _put_projection(self, bundle: HeadcountExtractionBundle) -> None:
        payload = bundle.payload
        selected = payload.selected
        spans = _all_evidence_spans(payload)
        self.connection.execute(
            """
            insert into staging.headcount_facts (
                normalized_record_id, extraction_invocation_id, issuer_id,
                availability, value, unit, scope, valid_period_end,
                transaction_time, recorded_at, confidence, review_status,
                unavailable_reason, evidence_spans, payload, record_ref, raw_ref
            ) values (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                bundle.record.normalized_record_id,
                bundle.invocation.extraction_invocation_id,
                bundle.record.draft.subject.id,
                payload.availability.value,
                None if selected is None else selected.value,
                None if selected is None else selected.unit,
                None if selected is None else selected.scope.value,
                payload.valid_period_end,
                bundle.record.draft.knowable_at,
                bundle.record.recorded_at,
                bundle.record.confidence,
                payload.review_status.value,
                payload.reason,
                Jsonb([span.model_dump(mode="json") for span in spans]),
                Jsonb(payload.model_dump(mode="json")),
                Jsonb(bundle.record.model_dump(mode="json")),
                bundle.raw_ref,
            ),
        )

    def _validate_existing_result(self, bundle: HeadcountExtractionBundle) -> None:
        row = self.connection.execute(
            """
            select normalized.record_ref, normalized.payload, normalized.raw_ref,
                   result.payload, result.record_ref, result.raw_ref, result.evidence_spans
            from staging.normalized_records normalized
            join staging.headcount_facts result
              on result.normalized_record_id = normalized.normalized_record_id
            where normalized.normalized_record_id = %s
            """,
            (bundle.record.normalized_record_id,),
        ).fetchone()
        spans = _all_evidence_spans(bundle.payload)
        expected = (
            bundle.record.model_dump(mode="json"),
            bundle.payload.model_dump(mode="json"),
            bundle.raw_ref,
            bundle.payload.model_dump(mode="json"),
            bundle.record.model_dump(mode="json"),
            bundle.raw_ref,
            [span.model_dump(mode="json") for span in spans],
        )
        if row is None or tuple(row) != expected:
            raise ValueError("normalized headcount ID is already bound to different content")


__all__ = ["PostgresHeadcountRepository"]
