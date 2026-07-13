"""Batch-private registry for the D1 filing-document vertical."""

from __future__ import annotations

import hashlib
from pathlib import Path

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.registries import (
    RegistrySnapshot,
    SemanticTypeRegistryEntry,
    SourceRegistryEntry,
)

from data_engine.mvp_models import FilingDocumentPayload

FILING_SOURCE_ID = "source.fixture-sec"
FILING_SEMANTIC_TYPE_ID = "semantic.filing-document"
FILING_VERSION = "1.0.0"


def _module_sha256(filename: str) -> str:
    return hashlib.sha256(Path(__file__).with_name(filename).read_bytes()).hexdigest()


def build_filing_registry() -> RegistrySnapshot:
    """Bind registry identities to the actual E0 model and implementation bytes."""

    payload_schema_sha256 = canonical_sha256(FilingDocumentPayload.model_json_schema())
    model_sha256 = _module_sha256("mvp_models.py")
    repository_sha256 = _module_sha256("mvp_repository.py")
    projector_sha256 = _module_sha256("mvp_snapshot.py")
    source_sha256 = _module_sha256("mvp_sources.py")
    semantic_type = SemanticTypeRegistryEntry(
        semantic_type_id=FILING_SEMANTIC_TYPE_ID,
        version=FILING_VERSION,
        domain=DataDomain.FILINGS,
        schema_version=FILING_VERSION,
        schema_fingerprint_sha256=payload_schema_sha256,
        normalized_model_key="data_engine:FilingDocumentPayload",
        input_model_key="factors:FilingDocumentInput",
        repository_key="data_engine:PostgresFilingDocumentRepository",
        projector_key="data_engine:build_filing_snapshot",
        compatibility_sha256=canonical_sha256({"compatible_schema_versions": []}),
        model_implementation_sha256=model_sha256,
        repository_implementation_sha256=repository_sha256,
        projector_implementation_sha256=projector_sha256,
    )
    source = SourceRegistryEntry(
        source_id=FILING_SOURCE_ID,
        version=FILING_VERSION,
        adapter_id="data_engine:FilingFixtureAdapter",
        adapter_version=FILING_VERSION,
        normalizer_id="data_engine:FilingDocumentNormalizer",
        normalizer_version=FILING_VERSION,
        supported_domains=(DataDomain.FILINGS,),
        supported_type_ids=(semantic_type.semantic_type_id,),
        configuration_schema_sha256=canonical_sha256({"type": "object", "additionalProperties": False}),
        mapping_schema_sha256=payload_schema_sha256,
        adapter_implementation_sha256=source_sha256,
        normalizer_implementation_sha256=source_sha256,
    )
    return RegistrySnapshot(
        sources=(source,),
        semantic_types=(semantic_type,),
        required_type_ids=(semantic_type.semantic_type_id,),
    )


__all__ = [
    "FILING_SEMANTIC_TYPE_ID",
    "FILING_SOURCE_ID",
    "FILING_VERSION",
    "build_filing_registry",
]
