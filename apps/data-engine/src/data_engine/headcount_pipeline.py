"""Frozen-corpus Local/CI pipeline for the H0 E1 evidence rung."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

from psycopg import Connection
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from truealpha_contracts import RawObjectStore
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import (
    ExtractionTemplate,
    ModelRevisionRef,
    NormalizedRecordRef,
    SemanticDraft,
    SemanticProducerKind,
)
from truealpha_contracts.models import DataSource, RawCapture
from truealpha_contracts.registries import RegistrySnapshot, SemanticTypeRegistryEntry, SourceRegistryEntry
from truealpha_contracts.universe import SubjectKind, SubjectRef

from data_engine.headcount_models import (
    D1_RUNTIME_HANDOFF_ID,
    D1_RUNTIME_HANDOFF_SHA256,
    HEADCOUNT_CORPUS_SHA256,
    EvidenceSpan,
    HeadcountAvailability,
    HeadcountCandidate,
    HeadcountExtractionBundle,
    HeadcountPayload,
    HeadcountReviewStatus,
    HeadcountScope,
    build_corpus_fixture_headcount_extraction,
    build_fixture_extraction_identity,
    build_handoff_member_fixture_headcount_extraction,
    visible_document_text,
)
from data_engine.headcount_registry import (
    HEADCOUNT_CORPUS_SOURCE_ID,
    HEADCOUNT_CORPUS_SOURCE_VERSION,
    build_headcount_registry,
)
from data_engine.headcount_repository import PostgresHeadcountRepository
from data_engine.mvp_assets import D1HandoffActivation, MvpNormalizationHandoff, run_d1_e2
from data_engine.mvp_models import FilingDocumentPayload
from data_engine.mvp_registry import FILING_SEMANTIC_TYPE_ID, FILING_VERSION
from data_engine.mvp_repository import PostgresFilingDocumentRepository
from data_engine.raw_store import get_payload, insert_fetch, raw_ref

DEFAULT_HEADCOUNT_CORPUS_PATH = Path("apps/data-engine/tests/fixtures/headcount_extraction/corpus.v1.json")
D1_GOVERNANCE_HANDOFF_ID = (
    "handoff:d1-mvp-normalization-handoff:6b7dee09f06996dc1635695c94c9802735ba10744964abef65e4c9f5caead7e7"
)
D1_GOVERNANCE_HANDOFF_SHA256 = "d872123a10fa626a5f777182b1f0c822c4013c0b4375ad10c6ea93da00716137"

_EXPECTED_ARTIFACT_IDS = {
    "plug-original-filing",
    "plug-amended-filing",
    "ddog-2025-filing",
    "nice-2025-filing",
    "jpm-2025-filing",
    "nvda-guidance-without-headcount",
}
_EXPECTED_CASE_IDS = {
    "d1-selected-plug-total",
    "ddog-total-versus-departments",
    "nice-worldwide-total-with-contractors",
    "missing-total-headcount",
    "jpm-financial-issuer-branch-input",
    "d1-pit-restatement-replay",
}
_PIT_CASE_ID = "d1-pit-restatement-replay"


def _resolve_inside(root: Path, relative_path: str) -> Path:
    resolved_root = root.resolve()
    resolved = (resolved_root / relative_path).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError(f"fixture path escapes repository: {relative_path}")
    return resolved


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _git_blob_sha1(body: bytes) -> str:
    header = f"blob {len(body)}\0".encode()
    return hashlib.sha1(header + body, usedforsecurity=False).hexdigest()


def _aware_datetime(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed


def _date(value: Any, *, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO date") from error


def _required_string(item: dict[str, Any], key: str, *, label: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}.{key} must be a non-empty string")
    return value


@dataclass(frozen=True)
class FrozenHeadcountArtifact:
    artifact_id: str
    path: str
    sha256: str
    git_blob: str
    subject_id: str
    cik: str
    accession: str
    form: str
    filing_date: date
    report_period: date
    accepted_at: datetime
    acceptance_source: str
    supersedes_artifact_id: str | None
    d1_handoff_member: bool
    body: bytes

    @property
    def source_record_id(self) -> str:
        return f"h0-fixture:{self.artifact_id}"


@dataclass(frozen=True)
class FrozenHeadcountCase:
    case_id: str
    artifact_ids: tuple[str, ...]
    expected: dict[str, Any]
    cutoffs: tuple[tuple[datetime, str], ...] = ()


@dataclass(frozen=True)
class FrozenHeadcountCorpus:
    corpus_id: str
    artifacts: tuple[FrozenHeadcountArtifact, ...]
    cases: tuple[FrozenHeadcountCase, ...]
    producer_handoff_id: str
    producer_handoff_sha256: str
    allowed_environments: tuple[str, ...]

    def artifact(self, artifact_id: str) -> FrozenHeadcountArtifact:
        try:
            return next(item for item in self.artifacts if item.artifact_id == artifact_id)
        except StopIteration as error:
            raise LookupError(artifact_id) from error

    def case(self, case_id: str) -> FrozenHeadcountCase:
        try:
            return next(item for item in self.cases if item.case_id == case_id)
        except StopIteration as error:
            raise LookupError(case_id) from error


def load_headcount_corpus(
    repository_root: Path,
    corpus_path: Path = DEFAULT_HEADCOUNT_CORPUS_PATH,
) -> FrozenHeadcountCorpus:
    path = _resolve_inside(repository_root, corpus_path.as_posix())
    corpus_bytes = path.read_bytes()
    if _sha256(corpus_bytes) != HEADCOUNT_CORPUS_SHA256:
        raise ValueError("H0 frozen corpus checksum drifted")
    value = json.loads(corpus_bytes)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("unsupported H0 frozen corpus")
    if value.get("corpus_id") != "h0-core-headcount-extraction-v1":
        raise ValueError("H0 frozen corpus identity drifted")

    producer = value.get("producer_handoff")
    if not isinstance(producer, dict):
        raise ValueError("H0 corpus producer handoff is missing")
    handoff_path = _required_string(producer, "path", label="producer_handoff")
    handoff_sha256 = _required_string(producer, "sha256", label="producer_handoff")
    handoff_id = _required_string(producer, "handoff_id", label="producer_handoff")
    handoff_bytes = _resolve_inside(repository_root, handoff_path).read_bytes()
    if handoff_sha256 != D1_GOVERNANCE_HANDOFF_SHA256 or _sha256(handoff_bytes) != handoff_sha256:
        raise ValueError("accepted D1 governance handoff checksum drifted")
    handoff = json.loads(handoff_bytes)
    allowed_environments = tuple(sorted(producer.get("allowed_environments", ())))
    if (
        handoff_id != D1_GOVERNANCE_HANDOFF_ID
        or handoff.get("handoff_id") != handoff_id
        or producer.get("state") != "accepted"
        or handoff.get("state") != "accepted"
        or producer.get("allowed_consumer") != "H0-core-headcount-extraction"
        or "H0-core-headcount-extraction" not in handoff.get("allowed_consumers", ())
        or allowed_environments != ("ci", "local")
        or tuple(sorted(handoff.get("allowed_environments", ()))) != allowed_environments
    ):
        raise ValueError("accepted D1 governance handoff does not authorize H0 Local/CI")

    fixture_extractor = value.get("fixture_extractor")
    policy_state = value.get("policy_state")
    isolation = value.get("release_isolation")
    if (
        not isinstance(fixture_extractor, dict)
        or fixture_extractor.get("network_calls") is not False
        or fixture_extractor.get("credentials") is not False
        or not isinstance(policy_state, dict)
        or "no stable semantic" not in str(policy_state.get("maximum_claim", ""))
        or not isinstance(isolation, dict)
        or any(isolation.values())
    ):
        raise ValueError("H0 corpus exceeds the provisional fixture-only source ceiling")

    raw_artifacts = value.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise ValueError("H0 corpus artifacts are missing")
    artifacts = tuple(_load_artifact(repository_root, item) for item in raw_artifacts)
    artifact_ids = {artifact.artifact_id for artifact in artifacts}
    if len(artifacts) != len(artifact_ids) or artifact_ids != _EXPECTED_ARTIFACT_IDS:
        raise ValueError("H0 frozen artifact set is incomplete or duplicated")
    for artifact in artifacts:
        if artifact.supersedes_artifact_id is not None and artifact.supersedes_artifact_id not in artifact_ids:
            raise ValueError(f"unknown predecessor for {artifact.artifact_id}")

    raw_cases = value.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("H0 corpus cases are missing")
    cases = tuple(_load_case(item, artifact_ids) for item in raw_cases)
    case_ids = {case.case_id for case in cases}
    if len(cases) != len(case_ids) or case_ids != _EXPECTED_CASE_IDS:
        raise ValueError("H0 frozen case set is incomplete or duplicated")
    replay_matrix = value.get("replay_matrix")
    mutations = (
        {item.get("mutation") for item in replay_matrix if isinstance(item, dict)}
        if isinstance(replay_matrix, list)
        else set()
    )
    if not {
        "none",
        "document-bytes",
        "model-revision",
        "instructions",
        "output-schema",
        "decoding-parameters",
    }.issubset(mutations):
        raise ValueError("H0 replay mutation matrix is incomplete")
    if tuple(value.get("execution_backends", ())) != ("fixture", "ephemeral-postgres"):
        raise ValueError("H0 E1 requires fixture and ephemeral-Postgres backends")
    return FrozenHeadcountCorpus(
        corpus_id=value["corpus_id"],
        artifacts=artifacts,
        cases=cases,
        producer_handoff_id=handoff_id,
        producer_handoff_sha256=handoff_sha256,
        allowed_environments=allowed_environments,
    )


def _load_artifact(repository_root: Path, value: Any) -> FrozenHeadcountArtifact:
    if not isinstance(value, dict):
        raise ValueError("H0 artifact entry must be an object")
    artifact_id = _required_string(value, "artifact_id", label="artifact")
    relative_path = _required_string(value, "path", label=artifact_id)
    expected_sha256 = _required_string(value, "sha256", label=artifact_id)
    expected_git_blob = _required_string(value, "git_blob", label=artifact_id)
    body = _resolve_inside(repository_root, relative_path).read_bytes()
    if _sha256(body) != expected_sha256 or _git_blob_sha1(body) != expected_git_blob:
        raise ValueError(f"H0 filing bytes drifted: {artifact_id}")
    subject_id = _required_string(value, "subject_id", label=artifact_id)
    cik = _required_string(value, "cik", label=artifact_id)
    accession = _required_string(value, "accession", label=artifact_id)
    acceptance_source = _required_string(value, "acceptance_source", label=artifact_id)
    if (
        not subject_id.startswith("issuer.")
        or len(cik) != 10
        or not cik.isdigit()
        or not acceptance_source.startswith("https://data.sec.gov/")
    ):
        raise ValueError(f"H0 filing identity evidence is invalid: {artifact_id}")
    supersedes = value.get("supersedes_artifact_id")
    if supersedes is not None and not isinstance(supersedes, str):
        raise ValueError(f"{artifact_id}.supersedes_artifact_id is invalid")
    if not isinstance(value.get("d1_handoff_member"), bool):
        raise ValueError(f"{artifact_id}.d1_handoff_member is invalid")
    return FrozenHeadcountArtifact(
        artifact_id=artifact_id,
        path=relative_path,
        sha256=expected_sha256,
        git_blob=expected_git_blob,
        subject_id=subject_id,
        cik=cik,
        accession=accession,
        form=_required_string(value, "form", label=artifact_id),
        filing_date=_date(value.get("filing_date"), label=f"{artifact_id}.filing_date"),
        report_period=_date(value.get("report_period"), label=f"{artifact_id}.report_period"),
        accepted_at=_aware_datetime(value.get("accepted_at"), label=f"{artifact_id}.accepted_at"),
        acceptance_source=acceptance_source,
        supersedes_artifact_id=supersedes,
        d1_handoff_member=value["d1_handoff_member"],
        body=body,
    )


def _load_case(value: Any, artifact_ids: set[str]) -> FrozenHeadcountCase:
    if not isinstance(value, dict):
        raise ValueError("H0 case entry must be an object")
    case_id = _required_string(value, "case_id", label="case")
    raw_artifact_ids = value.get("artifact_ids")
    expected = value.get("expected")
    if (
        not isinstance(raw_artifact_ids, list)
        or not raw_artifact_ids
        or any(not isinstance(item, str) for item in raw_artifact_ids)
        or not isinstance(expected, dict)
    ):
        raise ValueError(f"H0 case payload is invalid: {case_id}")
    selected_artifacts = tuple(raw_artifact_ids)
    if len(selected_artifacts) != len(set(selected_artifacts)) or not set(selected_artifacts) <= artifact_ids:
        raise ValueError(f"H0 case artifact references are invalid: {case_id}")
    raw_cutoffs = value.get("cutoffs", [])
    if not isinstance(raw_cutoffs, list):
        raise ValueError(f"H0 case cutoffs are invalid: {case_id}")
    cutoffs: list[tuple[datetime, str]] = []
    for index, cutoff in enumerate(raw_cutoffs):
        if not isinstance(cutoff, dict):
            raise ValueError(f"H0 case cutoff is invalid: {case_id}")
        selected = _required_string(cutoff, "selected_artifact_id", label=f"{case_id}.cutoffs[{index}]")
        if selected not in selected_artifacts:
            raise ValueError(f"H0 cutoff selects an artifact outside its case: {case_id}")
        cutoffs.append(
            (
                _aware_datetime(cutoff.get("as_of"), label=f"{case_id}.cutoffs[{index}].as_of"),
                selected,
            )
        )
    return FrozenHeadcountCase(
        case_id=case_id,
        artifact_ids=selected_artifacts,
        expected=dict(expected),
        cutoffs=tuple(cutoffs),
    )


class HeadcountCorpusAdapter:
    """Capture reviewed local bytes without a network or credential boundary."""

    def capture(self, artifact: FrozenHeadcountArtifact) -> RawCapture:
        if artifact.d1_handoff_member:
            raise ValueError("D1 handoff members must retain their accepted D1 source identity")
        if _sha256(artifact.body) != artifact.sha256:
            raise ValueError("H0 filing bytes changed after corpus validation")
        return RawCapture(
            source=DataSource.SEC,
            source_record_id=artifact.source_record_id,
            body=artifact.body,
            content_type="text/html",
            source_published_at=artifact.accepted_at,
            fetched_at=artifact.accepted_at + timedelta(seconds=30),
            metadata={
                "fixture_only": True,
                "artifact_id": artifact.artifact_id,
                "acceptance_source": artifact.acceptance_source,
                "accession": artifact.accession,
                "form": artifact.form,
            },
        )


class HeadcountCorpusDocumentNormalizer:
    """Create D1-schema filing records under an explicit H0 fixture source entry."""

    def normalize(
        self,
        artifact: FrozenHeadcountArtifact,
        raw_id: int,
        source_entry: SourceRegistryEntry,
        type_entry: SemanticTypeRegistryEntry,
    ) -> tuple[NormalizedRecordRef, FilingDocumentPayload]:
        if artifact.d1_handoff_member:
            raise ValueError("D1 handoff members cannot be renormalized by H0")
        if raw_id < 1 or _sha256(artifact.body) != artifact.sha256:
            raise ValueError("H0 corpus normalization raw identity is invalid")
        if (
            source_entry.key != (HEADCOUNT_CORPUS_SOURCE_ID, HEADCOUNT_CORPUS_SOURCE_VERSION)
            or type_entry.key != (FILING_SEMANTIC_TYPE_ID, FILING_VERSION)
            or FILING_SEMANTIC_TYPE_ID not in source_entry.supported_type_ids
        ):
            raise ValueError("H0 corpus document route is not bound by the additive registry")
        payload = FilingDocumentPayload(
            accession=artifact.accession,
            form=artifact.form,
            filing_date=artifact.filing_date,
            report_period=artifact.report_period,
            content_sha256=artifact.sha256,
            content_type="text/html",
        )
        draft = SemanticDraft(
            semantic_type_id=type_entry.semantic_type_id,
            semantic_type_version=type_entry.version,
            payload_model_key=type_entry.normalized_model_key,
            payload_schema_sha256=type_entry.schema_fingerprint_sha256,
            payload_sha256=canonical_sha256(payload.model_dump(mode="json")),
            subject=SubjectRef(kind=SubjectKind.ISSUER, id=artifact.subject_id),
            valid_from=date(artifact.report_period.year, 1, 1),
            valid_to=artifact.report_period,
            knowable_at=artifact.accepted_at,
            produced_at=artifact.accepted_at + timedelta(seconds=90),
            producer_kind=SemanticProducerKind.DETERMINISTIC_NORMALIZER,
            producer_id=source_entry.normalizer_id,
            producer_version=source_entry.normalizer_version,
            producer_implementation_sha256=source_entry.normalizer_implementation_sha256,
        )
        return (
            NormalizedRecordRef(
                draft=draft,
                document_id=f"document:{artifact.accession}",
                raw_object_id=f"raw-object:{artifact.sha256}",
                raw_object_sha256=artifact.sha256,
                source_registry_entry_id=source_entry.source_registry_entry_id,
                source_registry_entry_sha256=source_entry.content_sha256,
                mapping_version="h0-fixture-filing:0.1.0",
                mapping_implementation_sha256=source_entry.normalizer_implementation_sha256,
                recorded_at=artifact.accepted_at + timedelta(minutes=2),
                confidence=Decimal("0.98"),
            ),
            payload,
        )


class HeadcountFixtureExtractor(Protocol):
    def extract(
        self,
        case: FrozenHeadcountCase,
        artifact: FrozenHeadcountArtifact,
        document_record: NormalizedRecordRef,
        raw_body: bytes,
    ) -> HeadcountPayload: ...


class FrozenResponseExtractor:
    """Materialize the embedded reviewed response; this is not a live model call."""

    def extract(
        self,
        case: FrozenHeadcountCase,
        artifact: FrozenHeadcountArtifact,
        document_record: NormalizedRecordRef,
        raw_body: bytes,
    ) -> HeadcountPayload:
        expected = case.expected
        valid_period_end = _date(
            expected.get("valid_period_end", artifact.report_period.isoformat()),
            label=f"{case.case_id}.expected.valid_period_end",
        )
        confidence = Decimal(str(expected.get("confidence")))
        review_status = HeadcountReviewStatus(str(expected.get("review_status")))
        availability = HeadcountAvailability(str(expected.get("availability")))
        if availability is HeadcountAvailability.UNAVAILABLE:
            return HeadcountPayload(
                availability=availability,
                valid_period_end=valid_period_end,
                selected=None,
                candidates=(),
                confidence=confidence,
                review_status=review_status,
                reason=_required_string(expected, "reason", label=f"{case.case_id}.expected"),
            )

        document = visible_document_text(raw_body)
        raw_spans = expected.get("evidence_spans")
        if not isinstance(raw_spans, list) or not raw_spans:
            raise ValueError(f"available H0 case lacks evidence spans: {case.case_id}")
        selected_spans = tuple(self._span(document_record, document, item, case_id=case.case_id) for item in raw_spans)
        selected_value = int(_required_string(expected, "selected_value", label=f"{case.case_id}.expected"))
        selected = HeadcountCandidate(
            value=selected_value,
            scope=HeadcountScope.TOTAL,
            valid_period_end=valid_period_end,
            evidence_spans=selected_spans,
        )
        raw_rejected = expected.get("rejected_candidates", ())
        if not isinstance(raw_rejected, list):
            raise ValueError(f"H0 rejected candidate list is invalid: {case.case_id}")
        all_text = [
            _required_string(item, "text", label=f"{case.case_id}.evidence")
            for item in raw_spans
            if isinstance(item, dict)
        ]
        all_text.extend(
            item["evidence_text"]
            for item in raw_rejected
            if isinstance(item, dict) and isinstance(item.get("evidence_text"), str)
        )
        rejected: list[HeadcountCandidate] = []
        for index, item in enumerate(raw_rejected):
            if not isinstance(item, dict):
                raise ValueError(f"H0 rejected candidate is invalid: {case.case_id}")
            value = int(_required_string(item, "value", label=f"{case.case_id}.rejected[{index}]"))
            evidence_text = item.get("evidence_text")
            if not isinstance(evidence_text, str):
                formatted = f"{value:,}"
                evidence_text = next((text for text in all_text if formatted in text), None)
            if evidence_text is None:
                raise ValueError(f"H0 rejected candidate lacks exact evidence: {case.case_id}")
            rejected.append(
                HeadcountCandidate(
                    value=value,
                    scope=HeadcountScope(_required_string(item, "scope", label=f"{case.case_id}.rejected[{index}]")),
                    valid_period_end=valid_period_end,
                    evidence_spans=(
                        EvidenceSpan.locate(
                            document_id=document_record.document_id,
                            document=document,
                            text=evidence_text,
                        ),
                    ),
                )
            )
        return HeadcountPayload(
            availability=availability,
            valid_period_end=valid_period_end,
            selected=selected,
            candidates=(selected, *rejected),
            confidence=confidence,
            review_status=review_status,
        )

    @staticmethod
    def _span(
        document_record: NormalizedRecordRef,
        document,
        value: Any,
        *,
        case_id: str,
    ) -> EvidenceSpan:
        if not isinstance(value, dict) or value.get("view") != "visible-text-v1":
            raise ValueError(f"H0 evidence view is invalid: {case_id}")
        text = _required_string(value, "text", label=f"{case_id}.evidence")
        if value.get("occurrence_count") != document.text.count(text):
            raise ValueError(f"H0 evidence occurrence count drifted: {case_id}")
        return EvidenceSpan.locate(
            document_id=document_record.document_id,
            document=document,
            text=text,
        )


@dataclass(frozen=True)
class HeadcountDocument:
    artifact: FrozenHeadcountArtifact
    record: NormalizedRecordRef
    payload: FilingDocumentPayload
    raw_ref: str
    body: bytes


@dataclass(frozen=True)
class _E1Context:
    corpus: FrozenHeadcountCorpus
    handoff: MvpNormalizationHandoff
    activation: D1HandoffActivation
    registry: RegistrySnapshot
    corpus_source: SourceRegistryEntry
    documents: dict[str, HeadcountDocument]


class H0E1CaseEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    artifact_id: str
    source_document_record_id: str = Field(pattern=r"^normalized-record:[0-9a-f]{64}$")
    extraction_invocation_id: str = Field(pattern=r"^extraction-invocation:[0-9a-f]{64}$")
    normalized_record_id: str = Field(pattern=r"^normalized-record:[0-9a-f]{64}$")
    availability: HeadcountAvailability
    selected_value: int | None = Field(default=None, gt=0)
    evidence_span_ids: tuple[str, ...]
    replayed_without_calls: Literal[True] = True
    branch_input: Literal["financial-issuer"] | None = None
    factor_policy: Literal["not-decided-by-H0"] | None = None

    @model_validator(mode="after")
    def validate_result(self) -> H0E1CaseEvidence:
        if self.availability is HeadcountAvailability.AVAILABLE:
            if self.selected_value is None or not self.evidence_span_ids:
                raise ValueError("available E1 case lacks a selected value or exact evidence")
        elif self.selected_value is not None or self.evidence_span_ids:
            raise ValueError("unavailable E1 case cannot expose a value or evidence span")
        if (self.branch_input is None) != (self.factor_policy is None):
            raise ValueError("financial branch evidence must retain the H0 non-policy boundary")
        return self


class H0E1Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(default="", pattern=r"^(?:|h0-e1-evidence:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    batch_id: Literal["H0-core-headcount-extraction"] = "H0-core-headcount-extraction"
    rung: Literal["E1"] = "E1"
    environment: Literal["local", "ci"]
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    governance_handoff_id: str
    governance_handoff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_handoff_id: str = Field(pattern=r"^mvp-normalization-handoff:[0-9a-f]{64}$")
    runtime_handoff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_snapshot_id: str = Field(pattern=r"^registry-snapshot:[0-9a-f]{64}$")
    registry_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_results: tuple[H0E1CaseEvidence, ...]
    invocation_ids: tuple[str, ...]
    replayed_invocation_ids: tuple[str, ...]
    document_vintage_ids: tuple[str, str]
    headcount_vintage_ids: tuple[str, str]
    pit_selection_ids: tuple[str, str, str]
    persisted_result_count: int = Field(ge=1)
    live_source_calls: Literal[False] = False
    live_model_calls: Literal[False] = False
    release_activation: Literal[False] = False
    stable_handoff: Literal[False] = False
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("E1 evidence time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_and_identify(self) -> H0E1Evidence:
        results = tuple(sorted(self.case_results, key=lambda result: result.case_id))
        expected_result_ids = _EXPECTED_CASE_IDS - {_PIT_CASE_ID}
        if {result.case_id for result in results} != expected_result_ids:
            raise ValueError("H0 E1 case result matrix is incomplete")
        invocations = tuple(sorted(set(self.invocation_ids)))
        replayed = tuple(sorted(set(self.replayed_invocation_ids)))
        if len(invocations) != 6 or replayed != invocations or self.persisted_result_count != 6:
            raise ValueError("H0 E1 did not persist and replay exactly six document vintages")
        if (
            len(set(self.document_vintage_ids)) != 2
            or len(set(self.headcount_vintage_ids)) != 2
            or self.pit_selection_ids[0] == self.pit_selection_ids[1]
            or self.pit_selection_ids[1] != self.pit_selection_ids[2]
            or set(self.pit_selection_ids) != set(self.headcount_vintage_ids)
        ):
            raise ValueError("H0 E1 PIT/restatement boundary is incomplete")
        object.__setattr__(self, "case_results", results)
        object.__setattr__(self, "invocation_ids", invocations)
        object.__setattr__(self, "replayed_invocation_ids", replayed)
        payload = self.model_dump(mode="json", exclude={"evidence_id", "content_sha256"})
        expected_hash = canonical_sha256(payload)
        if self.content_sha256 and self.content_sha256 != expected_hash:
            raise ValueError("H0 E1 evidence hash does not match its content")
        if self.evidence_id and self.evidence_id != f"h0-e1-evidence:{expected_hash}":
            raise ValueError("H0 E1 evidence ID does not match its content")
        object.__setattr__(self, "content_sha256", expected_hash)
        object.__setattr__(self, "evidence_id", f"h0-e1-evidence:{expected_hash}")
        return self


def build_e1_fixture_extraction_identity() -> tuple[ModelRevisionRef, ExtractionTemplate]:
    return build_fixture_extraction_identity(template_version="0.1.1")


def run_headcount_e1(
    *,
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    environment: Literal["local", "ci"],
    extractor: HeadcountFixtureExtractor | None = None,
) -> H0E1Evidence:
    context = _prepare_context(
        repository_root=repository_root,
        connection=connection,
        raw_store=raw_store,
        environment=environment,
    )
    active_extractor = extractor or FrozenResponseExtractor()
    model_revision, template = build_e1_fixture_extraction_identity()
    plug_case = context.corpus.case("d1-selected-plug-total")
    original_document = context.documents["plug-original-filing"]
    amended_document = context.documents["plug-amended-filing"]
    original, _ = _run_or_replay(
        connection=connection,
        context=context,
        case=plug_case,
        document=original_document,
        model_revision=model_revision,
        template=template,
        extractor=active_extractor,
    )
    amended, _ = _run_or_replay(
        connection=connection,
        context=context,
        case=plug_case,
        document=amended_document,
        model_revision=model_revision,
        template=template,
        extractor=active_extractor,
        supersedes_record=original.record,
    )
    if amended.record.supersedes_record_id != original.record.normalized_record_id:
        raise ValueError("H0 E1 amended headcount did not append over the prior vintage")

    bundles: dict[str, HeadcountExtractionBundle] = {plug_case.case_id: amended}
    for case in sorted(context.corpus.cases, key=lambda item: item.case_id):
        if case.case_id in {plug_case.case_id, _PIT_CASE_ID}:
            continue
        if len(case.artifact_ids) != 1:
            raise ValueError(f"H0 E1 extraction case must bind one artifact: {case.case_id}")
        bundle, _ = _run_or_replay(
            connection=connection,
            context=context,
            case=case,
            document=context.documents[case.artifact_ids[0]],
            model_revision=model_revision,
            template=template,
            extractor=active_extractor,
        )
        bundles[case.case_id] = bundle

    all_bundles = (original, *bundles.values())
    repository = PostgresHeadcountRepository(connection)
    replayed = tuple(
        repository.load(
            bundle.invocation.extraction_invocation_id,
            model_revision=model_revision,
            template=template,
        )
        for bundle in all_bundles
    )
    if replayed != all_bundles:
        raise ValueError("stored H0 E1 replay changed a semantic or invocation identity")

    pit_case = context.corpus.case(_PIT_CASE_ID)
    pit_ids: list[str] = []
    for cutoff, selected_artifact_id in pit_case.cutoffs:
        selected = repository.select_pit(
            subject=original.record.draft.subject,
            source_registry_entry_id=original.record.source_registry_entry_id,
            valid_on=original.payload.valid_period_end,
            as_of=cutoff,
            model_revision=model_revision,
            template=template,
        )
        expected_bundle = original if selected_artifact_id == original_document.artifact.artifact_id else amended
        if selected != (expected_bundle,):
            raise ValueError("H0 E1 PIT selection disagrees with the frozen cutoff matrix")
        pit_ids.append(selected[0].record.normalized_record_id)

    case_results = tuple(
        _case_evidence(context.corpus.case(case_id), context.documents, bundle) for case_id, bundle in bundles.items()
    )
    record_ids = [bundle.record.normalized_record_id for bundle in all_bundles]
    persisted_count = connection.execute(
        "select count(*) from staging.headcount_facts where normalized_record_id = any(%s)",
        (record_ids,),
    ).fetchone()
    if persisted_count is None:
        raise ValueError("H0 E1 persisted result count is unavailable")
    return H0E1Evidence(
        environment=environment,
        corpus_sha256=HEADCOUNT_CORPUS_SHA256,
        governance_handoff_id=context.corpus.producer_handoff_id,
        governance_handoff_sha256=context.corpus.producer_handoff_sha256,
        runtime_handoff_id=context.handoff.handoff_id,
        runtime_handoff_sha256=context.handoff.content_sha256,
        registry_snapshot_id=context.registry.registry_snapshot_id,
        registry_snapshot_sha256=context.registry.content_sha256,
        case_results=case_results,
        invocation_ids=tuple(bundle.invocation.extraction_invocation_id for bundle in all_bundles),
        replayed_invocation_ids=tuple(bundle.invocation.extraction_invocation_id for bundle in replayed),
        document_vintage_ids=(
            original.source_document_record_id,
            amended.source_document_record_id,
        ),
        headcount_vintage_ids=(
            original.record.normalized_record_id,
            amended.record.normalized_record_id,
        ),
        pit_selection_ids=(pit_ids[0], pit_ids[1], pit_ids[2]),
        persisted_result_count=persisted_count[0],
        created_at=max(bundle.record.recorded_at for bundle in all_bundles) + timedelta(minutes=1),
    )


def replay_headcount_e1(
    connection: Connection[Any],
    evidence: H0E1Evidence,
) -> tuple[HeadcountExtractionBundle, ...]:
    """Replay E1 from stored rows only; no raw-store, source, or extractor argument exists."""

    model_revision, template = build_e1_fixture_extraction_identity()
    repository = PostgresHeadcountRepository(connection)
    return tuple(
        repository.load(
            invocation_id,
            model_revision=model_revision,
            template=template,
        )
        for invocation_id in evidence.invocation_ids
    )


def run_headcount_variant(
    *,
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    environment: Literal["local", "ci"],
    case_id: str,
    model_revision: ModelRevisionRef,
    template: ExtractionTemplate,
) -> HeadcountExtractionBundle:
    context = _prepare_context(
        repository_root=repository_root,
        connection=connection,
        raw_store=raw_store,
        environment=environment,
    )
    case = context.corpus.case(case_id)
    if case.case_id == _PIT_CASE_ID or len(case.artifact_ids) != 1:
        raise ValueError("variant extraction requires one non-PIT frozen case")
    bundle, _ = _run_or_replay(
        connection=connection,
        context=context,
        case=case,
        document=context.documents[case.artifact_ids[0]],
        model_revision=model_revision,
        template=template,
        extractor=FrozenResponseExtractor(),
    )
    return bundle


def _prepare_context(
    *,
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    environment: Literal["local", "ci"],
) -> _E1Context:
    corpus = load_headcount_corpus(repository_root)
    if environment not in corpus.allowed_environments:
        raise ValueError("H0 corpus does not allow the requested environment")
    activation = D1HandoffActivation(
        consumer="H0-core-headcount-extraction",
        environment=environment,
        expected_handoff_id=D1_RUNTIME_HANDOFF_ID,
        expected_handoff_sha256=D1_RUNTIME_HANDOFF_SHA256,
    )
    handoff = run_d1_e2(repository_root, connection, raw_store)
    if (
        handoff.handoff_id != activation.expected_handoff_id
        or handoff.content_sha256 != activation.expected_handoff_sha256
    ):
        raise ValueError("H0 E1 materialized a stale D1 runtime handoff")
    d1_registry = handoff.snapshot.registry_snapshot
    if (
        d1_registry.registry_snapshot_id != handoff.registry_snapshot_id
        or d1_registry.content_sha256 != handoff.registry_snapshot_sha256
    ):
        raise ValueError("D1 handoff registry identity is internally inconsistent")
    registry = build_headcount_registry(d1_registry)
    corpus_source = next(
        (
            entry
            for entry in registry.sources
            if entry.key == (HEADCOUNT_CORPUS_SOURCE_ID, HEADCOUNT_CORPUS_SOURCE_VERSION)
        ),
        None,
    )
    if corpus_source is None:
        raise ValueError("H0 additive fixture source is absent from the registry")
    documents = _materialize_documents(
        repository_root=repository_root,
        connection=connection,
        raw_store=raw_store,
        corpus=corpus,
        handoff=handoff,
        registry=registry,
        corpus_source=corpus_source,
    )
    return _E1Context(
        corpus=corpus,
        handoff=handoff,
        activation=activation,
        registry=registry,
        corpus_source=corpus_source,
        documents=documents,
    )


def _materialize_documents(
    *,
    repository_root: Path,
    connection: Connection[Any],
    raw_store: RawObjectStore,
    corpus: FrozenHeadcountCorpus,
    handoff: MvpNormalizationHandoff,
    registry: RegistrySnapshot,
    corpus_source: SourceRegistryEntry,
) -> dict[str, HeadcountDocument]:
    del repository_root
    filing_type = next(
        (entry for entry in registry.semantic_types if entry.key == (FILING_SEMANTIC_TYPE_ID, FILING_VERSION)),
        None,
    )
    if filing_type is None:
        raise ValueError("H0 additive registry lost the D1 filing semantic type")
    documents: dict[str, HeadcountDocument] = {}
    d1_rows = connection.execute(
        """
        select record_ref, payload, raw_ref
        from staging.normalized_records
        where normalized_record_id = any(%s)
        """,
        (list(handoff.normalized_record_ids),),
    ).fetchall()
    d1_by_accession: dict[str, tuple[NormalizedRecordRef, FilingDocumentPayload, str]] = {}
    for record_value, payload_value, stored_raw_ref in d1_rows:
        record = NormalizedRecordRef.model_validate(record_value)
        payload = FilingDocumentPayload.model_validate(payload_value)
        d1_by_accession[payload.accession] = (record, payload, stored_raw_ref)
    for artifact in corpus.artifacts:
        if artifact.d1_handoff_member:
            try:
                record, payload, stored_raw_ref = d1_by_accession[artifact.accession]
            except KeyError as error:
                raise ValueError(f"D1 handoff member is absent from Postgres: {artifact.artifact_id}") from error
            body = get_payload(
                connection,
                int(stored_raw_ref.split(":", 1)[1]),
                store=raw_store,
            )
            if record.normalized_record_id not in handoff.normalized_record_ids or body != artifact.body:
                raise ValueError(f"D1 handoff member drifted from the frozen H0 corpus: {artifact.artifact_id}")
            documents[artifact.artifact_id] = HeadcountDocument(
                artifact=artifact,
                record=record,
                payload=payload,
                raw_ref=stored_raw_ref,
                body=body,
            )
            continue

        capture = HeadcountCorpusAdapter().capture(artifact)
        fetch_id = insert_fetch(
            connection,
            source=capture.source,
            source_record_id=capture.source_record_id,
            body=capture.body,
            content_type=capture.content_type,
            fetched_at=capture.fetched_at,
            source_published_at=capture.source_published_at,
            metadata=capture.metadata,
            store=raw_store,
            recorded_at=artifact.accepted_at + timedelta(minutes=1),
        )
        landed = get_payload(connection, fetch_id, store=raw_store)
        if landed != artifact.body:
            raise ValueError(f"H0 fixture raw readback drifted: {artifact.artifact_id}")
        record, payload = HeadcountCorpusDocumentNormalizer().normalize(
            artifact,
            fetch_id,
            corpus_source,
            filing_type,
        )
        stored_raw_ref = raw_ref(fetch_id)
        PostgresFilingDocumentRepository(connection).put(record, payload, raw_ref=stored_raw_ref)
        documents[artifact.artifact_id] = HeadcountDocument(
            artifact=artifact,
            record=record,
            payload=payload,
            raw_ref=stored_raw_ref,
            body=landed,
        )
    if set(documents) != _EXPECTED_ARTIFACT_IDS:
        raise ValueError("H0 E1 did not materialize the complete frozen document set")
    return documents


def _run_or_replay(
    *,
    connection: Connection[Any],
    context: _E1Context,
    case: FrozenHeadcountCase,
    document: HeadcountDocument,
    model_revision: ModelRevisionRef,
    template: ExtractionTemplate,
    extractor: HeadcountFixtureExtractor,
    supersedes_record: NormalizedRecordRef | None = None,
) -> tuple[HeadcountExtractionBundle, bool]:
    repository = PostgresHeadcountRepository(connection)
    stored = repository.load_for_input(
        document.record,
        model_revision=model_revision,
        template=template,
    )
    expected_supersedes = None if supersedes_record is None else supersedes_record.normalized_record_id
    if stored is not None:
        if stored.record.supersedes_record_id != expected_supersedes:
            raise ValueError("stored H0 E1 replay has a different restatement predecessor")
        return stored, True

    payload = extractor.extract(case, document.artifact, document.record, document.body)
    started_at = document.record.recorded_at + timedelta(minutes=1)
    if document.artifact.d1_handoff_member:
        bundle = build_handoff_member_fixture_headcount_extraction(
            handoff=context.handoff,
            activation=context.activation,
            document_record=document.record,
            document_payload=document.payload,
            raw_body=document.body,
            raw_ref=document.raw_ref,
            payload=payload,
            started_at=started_at,
            completed_at=started_at + timedelta(seconds=1),
            model_revision=model_revision,
            template=template,
            supersedes_record=supersedes_record,
        )
    else:
        if supersedes_record is not None:
            raise ValueError("H0 external fixture cases do not declare a restatement chain")
        bundle = build_corpus_fixture_headcount_extraction(
            document_record=document.record,
            document_payload=document.payload,
            raw_body=document.body,
            raw_ref=document.raw_ref,
            expected_source_registry_entry_id=context.corpus_source.source_registry_entry_id,
            expected_source_registry_entry_sha256=context.corpus_source.content_sha256,
            payload=payload,
            started_at=started_at,
            completed_at=started_at + timedelta(seconds=1),
            model_revision=model_revision,
            template=template,
        )
    if not repository.put(bundle):
        raise ValueError("fresh H0 E1 extraction unexpectedly reused a normalized result")
    return bundle, False


def _case_evidence(
    case: FrozenHeadcountCase,
    documents: dict[str, HeadcountDocument],
    bundle: HeadcountExtractionBundle,
) -> H0E1CaseEvidence:
    artifact_id = case.artifact_ids[0]
    if bundle.source_document_record_id != documents[artifact_id].record.normalized_record_id:
        raise ValueError("H0 case evidence points at the wrong frozen document")
    selected = bundle.payload.selected
    spans = () if selected is None else tuple(span.evidence_span_id for span in selected.evidence_spans)
    branch_input = case.expected.get("branch_input")
    factor_policy = case.expected.get("factor_policy")
    return H0E1CaseEvidence(
        case_id=case.case_id,
        artifact_id=artifact_id,
        source_document_record_id=bundle.source_document_record_id,
        extraction_invocation_id=bundle.invocation.extraction_invocation_id,
        normalized_record_id=bundle.record.normalized_record_id,
        availability=bundle.payload.availability,
        selected_value=None if selected is None else selected.value,
        evidence_span_ids=spans,
        branch_input=branch_input,
        factor_policy=factor_policy,
    )


__all__ = [
    "DEFAULT_HEADCOUNT_CORPUS_PATH",
    "D1_GOVERNANCE_HANDOFF_ID",
    "D1_GOVERNANCE_HANDOFF_SHA256",
    "FrozenHeadcountArtifact",
    "FrozenHeadcountCase",
    "FrozenHeadcountCorpus",
    "FrozenResponseExtractor",
    "H0E1CaseEvidence",
    "H0E1Evidence",
    "HeadcountCorpusAdapter",
    "HeadcountCorpusDocumentNormalizer",
    "build_e1_fixture_extraction_identity",
    "load_headcount_corpus",
    "replay_headcount_e1",
    "run_headcount_e1",
    "run_headcount_variant",
]
