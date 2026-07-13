"""Batch-private registry extension for the H0 fixture extraction slice."""

from __future__ import annotations

import hashlib
from pathlib import Path

from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.registries import RegistrySnapshot, SemanticTypeRegistryEntry, SourceRegistryEntry

from data_engine.headcount_models import (
    HEADCOUNT_CORPUS_SHA256,
    HEADCOUNT_PAYLOAD_MODEL_KEY,
    HEADCOUNT_SEMANTIC_TYPE_ID,
    HEADCOUNT_SEMANTIC_TYPE_VERSION,
    HeadcountPayload,
)
from data_engine.mvp_registry import FILING_SEMANTIC_TYPE_ID, FILING_VERSION, build_filing_registry

HEADCOUNT_CORPUS_SOURCE_ID = "source.fixture-h0-sec-corpus"
HEADCOUNT_CORPUS_SOURCE_VERSION = "0.1.0"


def _module_sha256(filename: str) -> str:
    return hashlib.sha256(Path(__file__).with_name(filename).read_bytes()).hexdigest()


def build_headcount_registry(d1_registry: RegistrySnapshot) -> RegistrySnapshot:
    """Extend, without rewriting, the exact D1 registry used by the accepted handoff."""

    if d1_registry != build_filing_registry():
        raise ValueError("H0 requires the exact D1 filing registry snapshot")
    filing_type = next(
        (entry for entry in d1_registry.semantic_types if entry.key == (FILING_SEMANTIC_TYPE_ID, FILING_VERSION)),
        None,
    )
    if filing_type is None:
        raise ValueError("D1 registry does not expose the filing-document semantic type")

    pipeline_sha256 = _module_sha256("headcount_pipeline.py")
    models_sha256 = _module_sha256("headcount_models.py")
    repository_sha256 = _module_sha256("headcount_repository.py")
    corpus_source = SourceRegistryEntry(
        source_id=HEADCOUNT_CORPUS_SOURCE_ID,
        version=HEADCOUNT_CORPUS_SOURCE_VERSION,
        adapter_id="data_engine:HeadcountCorpusAdapter",
        adapter_version=HEADCOUNT_CORPUS_SOURCE_VERSION,
        normalizer_id="data_engine:HeadcountCorpusDocumentNormalizer",
        normalizer_version=HEADCOUNT_CORPUS_SOURCE_VERSION,
        supported_domains=(DataDomain.FILINGS,),
        supported_type_ids=(FILING_SEMANTIC_TYPE_ID,),
        configuration_schema_sha256=canonical_sha256(
            {
                "corpus_sha256": HEADCOUNT_CORPUS_SHA256,
                "network_calls": False,
                "credentials": False,
            }
        ),
        mapping_schema_sha256=filing_type.schema_fingerprint_sha256,
        adapter_implementation_sha256=pipeline_sha256,
        normalizer_implementation_sha256=pipeline_sha256,
    )
    headcount_type = SemanticTypeRegistryEntry(
        semantic_type_id=HEADCOUNT_SEMANTIC_TYPE_ID,
        version=HEADCOUNT_SEMANTIC_TYPE_VERSION,
        domain=DataDomain.FILING_EXTRACTIONS,
        schema_version=HEADCOUNT_SEMANTIC_TYPE_VERSION,
        schema_fingerprint_sha256=canonical_sha256(HeadcountPayload.model_json_schema()),
        normalized_model_key=HEADCOUNT_PAYLOAD_MODEL_KEY,
        input_model_key="factors:Fact",
        repository_key="data_engine:PostgresHeadcountRepository",
        projector_key="data_engine:HeadcountExtractionBundle.factor_input",
        compatibility_sha256=canonical_sha256({"compatible_schema_versions": []}),
        model_implementation_sha256=models_sha256,
        repository_implementation_sha256=repository_sha256,
        projector_implementation_sha256=models_sha256,
    )
    return RegistrySnapshot(
        parent_snapshot_id=d1_registry.registry_snapshot_id,
        sources=(*d1_registry.sources, corpus_source),
        semantic_types=(*d1_registry.semantic_types, headcount_type),
        identifier_types=d1_registry.identifier_types,
        required_type_ids=(*d1_registry.required_type_ids, HEADCOUNT_SEMANTIC_TYPE_ID),
        required_identifier_type_ids=d1_registry.required_identifier_type_ids,
    )


__all__ = [
    "HEADCOUNT_CORPUS_SOURCE_ID",
    "HEADCOUNT_CORPUS_SOURCE_VERSION",
    "build_headcount_registry",
]
