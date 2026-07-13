"""Typed, fixture-only H0 headcount extraction slice."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from html.parser import HTMLParser
from typing import Literal

from factors import Fact
from pydantic import BaseModel, ConfigDict, Field, model_validator
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import (
    ExtractionInvocation,
    ExtractionTemplate,
    ModelRevisionRef,
    NormalizedRecordRef,
    SemanticDraft,
    SemanticProducerKind,
    validate_extraction_replay,
)

from data_engine.mvp_assets import D1HandoffActivation, MvpNormalizationHandoff
from data_engine.mvp_models import FilingDocumentPayload

HEADCOUNT_SEMANTIC_TYPE_ID = "semantic.employee-headcount"
HEADCOUNT_SEMANTIC_TYPE_VERSION = "0.1.0"
HEADCOUNT_PAYLOAD_MODEL_KEY = "data_engine:HeadcountPayload"
HEADCOUNT_CORPUS_SHA256 = "621cb9ccb6822acc497e57ccec669ac623228681747182178b1584a3234a15cf"
D1_RUNTIME_HANDOFF_SHA256 = "594dce80771bf98cf940f477ca9889d453a2ee8f66b8b6b51d4d10578c0a4a8c"
D1_RUNTIME_HANDOFF_ID = f"mvp-normalization-handoff:{D1_RUNTIME_HANDOFF_SHA256}"

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_STABLE_KEY_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9._:/@+-]*$"


def _identify(model: BaseModel, *, id_field: str, hash_field: str, prefix: str) -> None:
    payload = model.model_dump(mode="json", exclude={id_field, hash_field})
    expected_hash = canonical_sha256(payload)
    expected_id = f"{prefix}:{expected_hash}"
    supplied_hash = getattr(model, hash_field)
    supplied_id = getattr(model, id_field)
    if supplied_hash and supplied_hash != expected_hash:
        raise ValueError(f"{hash_field} does not match canonical content")
    if supplied_id and supplied_id != expected_id:
        raise ValueError(f"{id_field} does not match canonical content")
    object.__setattr__(model, hash_field, expected_hash)
    object.__setattr__(model, id_field, expected_id)


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in {"script", "style"}:
            self.ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


class VisibleDocumentText(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    view: Literal["visible-text-v1"] = "visible-text-v1"
    document_sha256: str = Field(pattern=_SHA256_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_content_hash(self) -> VisibleDocumentText:
        if hashlib.sha256(self.text.encode()).hexdigest() != self.content_sha256:
            raise ValueError("visible document text hash does not match")
        return self


def visible_document_text(body: bytes) -> VisibleDocumentText:
    parser = _VisibleTextParser()
    parser.feed(body.decode("utf-8", errors="ignore"))
    parser.close()
    text = " ".join(" ".join(parser.parts).split())
    if not text:
        raise ValueError("filing produced no visible text")
    return VisibleDocumentText(
        document_sha256=hashlib.sha256(body).hexdigest(),
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
    )


class HeadcountScope(StrEnum):
    TOTAL = "total"
    DEPARTMENTAL = "departmental"
    GEOGRAPHIC = "geographic"
    CONTRACTOR = "contractor"
    TEMPORARY = "temporary"
    OTHER = "other"


class HeadcountAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class HeadcountReviewStatus(StrEnum):
    REVIEWED_FIXTURE = "reviewed-fixture"
    NEEDS_REVIEW = "needs-review"
    REJECTED = "rejected"


class EvidenceSpan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_span_id: str = Field(default="", pattern=r"^(?:|evidence-span:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    document_id: str = Field(pattern=_STABLE_KEY_PATTERN)
    document_sha256: str = Field(pattern=_SHA256_PATTERN)
    text_view: Literal["visible-text-v1"] = "visible-text-v1"
    text_view_sha256: str = Field(pattern=_SHA256_PATTERN)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_span(self) -> EvidenceSpan:
        if self.end_char <= self.start_char or self.end_char - self.start_char != len(self.text):
            raise ValueError("evidence span offsets do not match its text")
        _identify(self, id_field="evidence_span_id", hash_field="content_sha256", prefix="evidence-span")
        return self

    @classmethod
    def locate(
        cls,
        *,
        document_id: str,
        document: VisibleDocumentText,
        text: str,
    ) -> EvidenceSpan:
        if document.text.count(text) != 1:
            raise ValueError("evidence text must occur exactly once in the normalized document")
        start = document.text.index(text)
        return cls(
            document_id=document_id,
            document_sha256=document.document_sha256,
            text_view_sha256=document.content_sha256,
            start_char=start,
            end_char=start + len(text),
            text=text,
        )


class HeadcountCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(default="", pattern=r"^(?:|headcount-candidate:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    value: int = Field(gt=0)
    unit: Literal["employees"] = "employees"
    scope: HeadcountScope
    valid_period_end: date
    evidence_spans: tuple[EvidenceSpan, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_candidate(self) -> HeadcountCandidate:
        coordinates = {(span.document_id, span.document_sha256) for span in self.evidence_spans}
        if len(coordinates) != 1:
            raise ValueError("a headcount candidate cannot mix source documents")
        spans = tuple(sorted(self.evidence_spans, key=lambda span: span.evidence_span_id))
        if len(spans) != len({span.evidence_span_id for span in spans}):
            raise ValueError("headcount candidate evidence spans must be unique")
        object.__setattr__(self, "evidence_spans", spans)
        _identify(self, id_field="candidate_id", hash_field="content_sha256", prefix="headcount-candidate")
        return self


class HeadcountPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    payload_id: str = Field(default="", pattern=r"^(?:|headcount-payload:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    availability: HeadcountAvailability
    valid_period_end: date
    selected: HeadcountCandidate | None
    candidates: tuple[HeadcountCandidate, ...] = ()
    confidence: Decimal = Field(ge=0, le=1)
    review_status: HeadcountReviewStatus
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_selection(self) -> HeadcountPayload:
        candidates = tuple(sorted(self.candidates, key=lambda candidate: candidate.candidate_id))
        if len(candidates) != len({candidate.candidate_id for candidate in candidates}):
            raise ValueError("headcount candidates must be unique")
        if any(candidate.valid_period_end != self.valid_period_end for candidate in candidates):
            raise ValueError("headcount candidates must share the payload valid period")
        if self.availability is HeadcountAvailability.AVAILABLE:
            if self.selected is None or self.selected.scope is not HeadcountScope.TOTAL:
                raise ValueError("an available headcount payload must select a total-employee candidate")
            if self.selected.candidate_id not in {candidate.candidate_id for candidate in candidates}:
                raise ValueError("selected headcount candidate is absent from the candidate set")
            if self.reason is not None:
                raise ValueError("an available headcount payload cannot carry an unavailable reason")
        elif self.selected is not None or self.reason is None:
            raise ValueError("an unavailable headcount payload requires a reason and no selected value")
        object.__setattr__(self, "candidates", candidates)
        _identify(self, id_field="payload_id", hash_field="content_sha256", prefix="headcount-payload")
        return self


class HeadcountExtractionBundle(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_document_record_id: str = Field(pattern=r"^normalized-record:[0-9a-f]{64}$")
    raw_ref: str = Field(pattern=r"^raw\.fetches:[1-9][0-9]*$")
    model_revision: ModelRevisionRef
    template: ExtractionTemplate
    invocation: ExtractionInvocation
    record: NormalizedRecordRef
    payload: HeadcountPayload

    @model_validator(mode="after")
    def validate_bundle(self) -> HeadcountExtractionBundle:
        validate_extraction_replay(
            draft=self.record.draft,
            invocation=self.invocation,
            template=self.template,
            model_revision=self.model_revision,
        )
        if (
            self.invocation.semantic_payload_sha256 != self.payload.content_sha256
            or self.record.draft.payload_sha256 != self.payload.content_sha256
            or self.record.confidence != self.payload.confidence
            or self.record.draft.semantic_type_id != HEADCOUNT_SEMANTIC_TYPE_ID
            or self.record.draft.semantic_type_version != HEADCOUNT_SEMANTIC_TYPE_VERSION
            or self.record.draft.payload_model_key != HEADCOUNT_PAYLOAD_MODEL_KEY
        ):
            raise ValueError("headcount result does not match its payload or extraction identity")
        return self

    def factor_input(self, *, as_of: datetime) -> Fact:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("factor input cutoff must be timezone-aware")
        if as_of < self.record.draft.knowable_at:
            raise ValueError("future headcount knowledge cannot enter a factor input")
        selected = self.payload.selected
        if self.payload.availability is not HeadcountAvailability.AVAILABLE or selected is None:
            raise ValueError("unavailable headcount cannot become a usable factor input")
        return Fact(
            entity_id=self.record.draft.subject.id,
            metric="employee_headcount",
            value=Decimal(selected.value),
            confidence=self.record.confidence,
            as_of=as_of,
            fiscal_period=self.payload.valid_period_end.isoformat(),
        )


def validate_d1_handoff_document(
    *,
    handoff: MvpNormalizationHandoff,
    activation: D1HandoffActivation,
    document_record: NormalizedRecordRef,
    document_payload: FilingDocumentPayload,
    raw_body: bytes,
) -> VisibleDocumentText:
    if handoff.handoff_id != D1_RUNTIME_HANDOFF_ID or handoff.content_sha256 != D1_RUNTIME_HANDOFF_SHA256:
        raise ValueError("H0 requires the exact accepted D1 runtime handoff")
    if (
        activation.expected_handoff_id != handoff.handoff_id
        or activation.expected_handoff_sha256 != handoff.content_sha256
        or activation.consumer != "H0-core-headcount-extraction"
        or activation.environment not in handoff.allowed_environments
        or activation.consumer not in handoff.allowed_consumers
    ):
        raise ValueError("D1 handoff activation does not authorize H0")
    records = {record.normalized_record_id: record for record in handoff.snapshot.normalized_records}
    if (
        handoff.selected_record_id != document_record.normalized_record_id
        or records.get(handoff.selected_record_id) != document_record
    ):
        raise ValueError("headcount document is not the D1 handoff selection")
    raw_sha256 = hashlib.sha256(raw_body).hexdigest()
    if (
        raw_sha256 != document_payload.content_sha256
        or raw_sha256 != document_record.raw_object_sha256
        or document_record.document_id != f"document:{document_payload.accession}"
        or document_record.draft.payload_sha256 != canonical_sha256(document_payload.model_dump(mode="json"))
    ):
        raise ValueError("D1 filing payload, record, and raw bytes do not match")
    return visible_document_text(raw_body)


def build_fixture_extraction_identity() -> tuple[ModelRevisionRef, ExtractionTemplate]:
    model_revision = ModelRevisionRef(
        provider="fixture",
        model_id="headcount-golden",
        immutable_revision="2026-07-13.v1",
        endpoint_or_artifact_sha256=HEADCOUNT_CORPUS_SHA256,
        decoding_parameters_sha256=canonical_sha256(
            {"temperature": "0", "top_p": "1", "response_mode": "frozen-fixture"}
        ),
    )
    template = ExtractionTemplate(
        template_name="total-employee-headcount",
        template_version="0.1.0",
        semantic_type_id=HEADCOUNT_SEMANTIC_TYPE_ID,
        semantic_type_version=HEADCOUNT_SEMANTIC_TYPE_VERSION,
        payload_model_key=HEADCOUNT_PAYLOAD_MODEL_KEY,
        output_schema_sha256=canonical_sha256(HeadcountPayload.model_json_schema()),
        instructions_sha256=canonical_sha256(
            {
                "instructions": (
                    "Select only a disclosed total employee count; retain partial, geographic, temporary, and "
                    "contractor candidates without promoting them to total."
                )
            }
        ),
        extractor_implementation_sha256=canonical_sha256(
            {"implementation": "data_engine:build_fixture_headcount_extraction", "version": "0.1.0"}
        ),
        model_revision_id=model_revision.model_revision_id,
        model_revision_sha256=model_revision.content_sha256,
    )
    return model_revision, template


def build_fixture_headcount_extraction(
    *,
    handoff: MvpNormalizationHandoff,
    activation: D1HandoffActivation,
    document_record: NormalizedRecordRef,
    document_payload: FilingDocumentPayload,
    raw_body: bytes,
    raw_ref: str,
    payload: HeadcountPayload,
    started_at: datetime,
    completed_at: datetime,
) -> HeadcountExtractionBundle:
    visible = validate_d1_handoff_document(
        handoff=handoff,
        activation=activation,
        document_record=document_record,
        document_payload=document_payload,
        raw_body=raw_body,
    )
    if payload.valid_period_end != document_payload.report_period:
        raise ValueError("headcount valid period does not match the D1 filing report period")
    for candidate in payload.candidates:
        for span in candidate.evidence_spans:
            if (
                span.document_id != document_record.document_id
                or span.document_sha256 != visible.document_sha256
                or span.text_view_sha256 != visible.content_sha256
                or visible.text[span.start_char : span.end_char] != span.text
            ):
                raise ValueError("headcount evidence span does not reproduce the D1 document")
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("extraction start must be timezone-aware")
    if completed_at.tzinfo is None or completed_at.utcoffset() is None:
        raise ValueError("extraction completion must be timezone-aware")
    if started_at < document_record.recorded_at:
        raise ValueError("headcount extraction cannot start before the D1 filing is recorded")

    model_revision, template = build_fixture_extraction_identity()
    invocation = ExtractionInvocation(
        model_revision_id=model_revision.model_revision_id,
        model_revision_sha256=model_revision.content_sha256,
        extraction_template_id=template.extraction_template_id,
        extraction_template_sha256=template.content_sha256,
        input_sha256=canonical_sha256(
            {
                "document_normalized_record_id": document_record.normalized_record_id,
                "document_payload_sha256": document_record.draft.payload_sha256,
                "document_content_sha256": document_record.raw_object_sha256,
            }
        ),
        response_sha256=canonical_sha256({"fixture_response": payload.model_dump(mode="json")}),
        semantic_payload_sha256=payload.content_sha256,
        attempt_number=1,
        started_at=started_at,
        completed_at=completed_at,
        invoker_id="data_engine:FixtureHeadcountExtractor",
        invoker_version="0.1.0",
        invoker_implementation_sha256=template.extractor_implementation_sha256,
    )
    draft = SemanticDraft(
        semantic_type_id=HEADCOUNT_SEMANTIC_TYPE_ID,
        semantic_type_version=HEADCOUNT_SEMANTIC_TYPE_VERSION,
        payload_model_key=HEADCOUNT_PAYLOAD_MODEL_KEY,
        payload_schema_sha256=template.output_schema_sha256,
        payload_sha256=payload.content_sha256,
        subject=document_record.draft.subject,
        valid_from=date(payload.valid_period_end.year, 1, 1),
        valid_to=payload.valid_period_end,
        knowable_at=document_record.draft.knowable_at,
        produced_at=completed_at,
        producer_kind=SemanticProducerKind.VERSIONED_EXTRACTION,
        producer_id=invocation.invoker_id,
        producer_version=invocation.invoker_version,
        producer_implementation_sha256=invocation.invoker_implementation_sha256,
        model_revision_id=model_revision.model_revision_id,
        model_revision_sha256=model_revision.content_sha256,
        extraction_template_id=template.extraction_template_id,
        extraction_template_sha256=template.content_sha256,
        extraction_invocation_id=invocation.extraction_invocation_id,
        extraction_invocation_sha256=invocation.content_sha256,
    )
    record = NormalizedRecordRef(
        draft=draft,
        document_id=document_record.document_id,
        raw_object_id=document_record.raw_object_id,
        raw_object_sha256=document_record.raw_object_sha256,
        source_registry_entry_id=document_record.source_registry_entry_id,
        source_registry_entry_sha256=document_record.source_registry_entry_sha256,
        mapping_version="fixture-headcount-extraction:0.1.0",
        mapping_implementation_sha256=template.extractor_implementation_sha256,
        recorded_at=completed_at + timedelta(seconds=1),
        confidence=payload.confidence,
    )
    return HeadcountExtractionBundle(
        source_document_record_id=document_record.normalized_record_id,
        raw_ref=raw_ref,
        model_revision=model_revision,
        template=template,
        invocation=invocation,
        record=record,
        payload=payload,
    )


__all__ = [
    "D1_RUNTIME_HANDOFF_ID",
    "D1_RUNTIME_HANDOFF_SHA256",
    "EvidenceSpan",
    "HEADCOUNT_CORPUS_SHA256",
    "HEADCOUNT_PAYLOAD_MODEL_KEY",
    "HEADCOUNT_SEMANTIC_TYPE_ID",
    "HEADCOUNT_SEMANTIC_TYPE_VERSION",
    "HeadcountAvailability",
    "HeadcountCandidate",
    "HeadcountExtractionBundle",
    "HeadcountPayload",
    "HeadcountReviewStatus",
    "HeadcountScope",
    "VisibleDocumentText",
    "build_fixture_extraction_identity",
    "build_fixture_headcount_extraction",
    "validate_d1_handoff_document",
    "visible_document_text",
]
