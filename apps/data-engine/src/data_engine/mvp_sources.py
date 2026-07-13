"""Frozen SEC fixture adapter and deterministic filing normalizer for D1."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.execution import (
    NormalizedRecordRef,
    SemanticDraft,
    SemanticProducerKind,
)
from truealpha_contracts.models import DataSource, RawCapture
from truealpha_contracts.registries import RegistrySnapshot
from truealpha_contracts.universe import SubjectKind, SubjectRef

from data_engine.mvp_models import FilingDocumentPayload
from data_engine.mvp_registry import FILING_SEMANTIC_TYPE_ID

FROZEN_CORPUS_SHA256 = "82f92f5c65d8cbe9e5a26fb1182e0584de9a0bb4d51f879ed3ea41e522e98ef1"


@dataclass(frozen=True)
class _FilingMetadata:
    accession: str
    form: str
    report_period: date
    supersedes_artifact_id: str | None


_FILING_METADATA = {
    "plug-original-filing": _FilingMetadata(
        accession="0001558370-21-007147",
        form="10-K",
        report_period=date(2020, 12, 31),
        supersedes_artifact_id=None,
    ),
    "plug-amended-filing": _FilingMetadata(
        accession="0001558370-22-003577",
        form="10-K/A",
        report_period=date(2020, 12, 31),
        supersedes_artifact_id="plug-original-filing",
    ),
}


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _resolve_inside(root: Path, relative_path: str) -> Path:
    root = root.resolve()
    path = (root / relative_path).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"fixture path escapes repository: {relative_path}")
    return path


def _aware_datetime(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed


@dataclass(frozen=True)
class FrozenFilingArtifact:
    artifact_id: str
    path: str
    sha256: str
    accepted_at: datetime
    acceptance_source: str
    accession: str
    form: str
    report_period: date
    supersedes_artifact_id: str | None
    body: bytes

    @property
    def source_record_id(self) -> str:
        return f"fixture:{self.artifact_id}"


class FilingFixtureAdapter:
    """Load only the frozen PLUG filing pair from the reviewed D0 corpus."""

    def load(self, root: Path, corpus_path: Path) -> tuple[FrozenFilingArtifact, ...]:
        path = _resolve_inside(root, corpus_path.as_posix())
        corpus_bytes = path.read_bytes()
        if _sha256(corpus_bytes) != FROZEN_CORPUS_SHA256:
            raise ValueError("D0 fixture corpus checksum drifted")
        corpus = json.loads(corpus_bytes)
        if not isinstance(corpus, dict) or corpus.get("schema_version") != 1:
            raise ValueError("unsupported D0 fixture corpus")
        self._validate_source_manifest(root, corpus.get("source_manifest"))
        artifacts = corpus.get("artifacts")
        if not isinstance(artifacts, list):
            raise ValueError("fixture corpus artifacts are missing")
        by_id = {
            item.get("artifact_id"): item
            for item in artifacts
            if isinstance(item, dict) and item.get("artifact_id") in _FILING_METADATA
        }
        if set(by_id) != set(_FILING_METADATA):
            raise ValueError("frozen PLUG filing pair is incomplete")
        return tuple(self._load_artifact(root, by_id[artifact_id]) for artifact_id in _FILING_METADATA)

    def capture(self, artifact: FrozenFilingArtifact) -> RawCapture:
        if _sha256(artifact.body) != artifact.sha256:
            raise ValueError("filing bytes changed after fixture validation")
        return RawCapture(
            source=DataSource.SEC,
            source_record_id=artifact.source_record_id,
            body=artifact.body,
            content_type="text/html",
            source_published_at=artifact.accepted_at,
            fetched_at=artifact.accepted_at + timedelta(seconds=30),
            metadata={
                "artifact_id": artifact.artifact_id,
                "acceptance_source": artifact.acceptance_source,
                "accession": artifact.accession,
                "form": artifact.form,
            },
        )

    def _validate_source_manifest(self, root: Path, value: Any) -> None:
        if not isinstance(value, dict):
            raise ValueError("fixture source manifest is missing")
        manifest_path = value.get("path")
        manifest_sha256 = value.get("sha256")
        if not isinstance(manifest_path, str) or not isinstance(manifest_sha256, str):
            raise ValueError("fixture source manifest reference is invalid")
        if _sha256(_resolve_inside(root, manifest_path).read_bytes()) != manifest_sha256:
            raise ValueError("fixture source manifest checksum drifted")

    def _load_artifact(self, root: Path, item: dict[str, Any]) -> FrozenFilingArtifact:
        artifact_id = item.get("artifact_id")
        if not isinstance(artifact_id, str):
            raise ValueError("fixture filing artifact ID is invalid")
        metadata = _FILING_METADATA.get(artifact_id)
        if metadata is None or item.get("source") != "sec" or item.get("semantic_type") != "filing-document":
            raise ValueError("fixture filing source or semantic type drifted")
        relative_path = item.get("path")
        expected_sha256 = item.get("sha256")
        acceptance_source = item.get("acceptance_source")
        if not isinstance(relative_path, str) or not isinstance(expected_sha256, str):
            raise ValueError("fixture filing path or checksum is invalid")
        if not isinstance(acceptance_source, str) or not acceptance_source.startswith("https://data.sec.gov/"):
            raise ValueError("fixture filing acceptance evidence is invalid")
        body = _resolve_inside(root, relative_path).read_bytes()
        if _sha256(body) != expected_sha256:
            raise ValueError(f"fixture filing checksum drifted: {artifact_id}")
        if b"0001093691" not in body:
            raise ValueError(f"fixture filing issuer identity drifted: {artifact_id}")
        return FrozenFilingArtifact(
            artifact_id=artifact_id,
            path=relative_path,
            sha256=expected_sha256,
            accepted_at=_aware_datetime(item.get("accepted_at"), label=f"{artifact_id}.accepted_at"),
            acceptance_source=acceptance_source,
            accession=metadata.accession,
            form=metadata.form,
            report_period=metadata.report_period,
            supersedes_artifact_id=metadata.supersedes_artifact_id,
            body=body,
        )


class FilingDocumentNormalizer:
    def normalize(
        self,
        artifact: FrozenFilingArtifact,
        raw_id: int,
        raw_sha256: str,
        registry: RegistrySnapshot,
        supersedes: NormalizedRecordRef | None = None,
    ) -> tuple[NormalizedRecordRef, FilingDocumentPayload]:
        if raw_id < 1:
            raise ValueError("raw fetch ID must be positive")
        if raw_sha256 != artifact.sha256 or _sha256(artifact.body) != raw_sha256:
            raise ValueError("normalization raw checksum does not match the frozen filing")
        source_entry = registry.sources[0]
        type_entry = registry.semantic_types[0]
        if type_entry.semantic_type_id != FILING_SEMANTIC_TYPE_ID:
            raise ValueError("registry does not bind the filing semantic type")
        if FILING_SEMANTIC_TYPE_ID not in source_entry.supported_type_ids:
            raise ValueError("registry source does not support filing documents")
        payload = FilingDocumentPayload(
            accession=artifact.accession,
            form=artifact.form,
            filing_date=artifact.accepted_at.date(),
            report_period=artifact.report_period,
            content_sha256=artifact.sha256,
            content_type="text/html",
        )
        subject = SubjectRef(kind=SubjectKind.ISSUER, id="issuer.plug")
        valid_from = date(artifact.report_period.year, 1, 1)
        if supersedes is not None:
            if supersedes.draft.subject != subject or supersedes.draft.valid_from != valid_from:
                raise ValueError("filing amendment predecessor belongs to another semantic coordinate")
            if supersedes.draft.valid_to != artifact.report_period:
                raise ValueError("filing amendment predecessor covers another report period")
            if supersedes.draft.knowable_at >= artifact.accepted_at:
                raise ValueError("filing amendment must become knowable after its predecessor")
        draft = SemanticDraft(
            semantic_type_id=type_entry.semantic_type_id,
            semantic_type_version=type_entry.version,
            payload_model_key=type_entry.normalized_model_key,
            payload_schema_sha256=type_entry.schema_fingerprint_sha256,
            payload_sha256=canonical_sha256(payload.model_dump(mode="json")),
            subject=subject,
            valid_from=valid_from,
            valid_to=artifact.report_period,
            knowable_at=artifact.accepted_at,
            produced_at=artifact.accepted_at + timedelta(seconds=90),
            producer_kind=SemanticProducerKind.DETERMINISTIC_NORMALIZER,
            producer_id=source_entry.normalizer_id,
            producer_version=source_entry.normalizer_version,
            producer_implementation_sha256=source_entry.normalizer_implementation_sha256,
        )
        record = NormalizedRecordRef(
            draft=draft,
            document_id=f"document:{artifact.accession}",
            raw_object_id=f"raw-object:{artifact.sha256}",
            raw_object_sha256=artifact.sha256,
            source_registry_entry_id=source_entry.source_registry_entry_id,
            source_registry_entry_sha256=source_entry.content_sha256,
            mapping_version="fixture-sec-filing:1.0.0",
            mapping_implementation_sha256=source_entry.normalizer_implementation_sha256,
            recorded_at=artifact.accepted_at + timedelta(minutes=2),
            confidence=Decimal("0.98"),
            is_restatement=supersedes is not None,
            supersedes_record_id=None if supersedes is None else supersedes.normalized_record_id,
        )
        return record, payload


__all__ = [
    "FROZEN_CORPUS_SHA256",
    "FilingDocumentNormalizer",
    "FilingFixtureAdapter",
    "FrozenFilingArtifact",
]
