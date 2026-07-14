"""Additive shared registry for the D2 medium-domain handoff."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.market import CorporateAction
from truealpha_contracts.models import FinancialFact
from truealpha_contracts.registries import (
    RegistryHistory,
    RegistrySnapshot,
    SemanticTypeRegistryEntry,
    SourceRegistryEntry,
)
from truealpha_contracts.universe import IssuerSecurityLink, SecurityListingLink, UniverseMembership

from data_engine.mvp_medium_models import MarketPricePayload
from data_engine.mvp_registry import build_filing_registry

MEDIUM_VERSION = "1.0.0"
ISSUER_SECURITY_TYPE_ID = "semantic.issuer-security-link"
SECURITY_LISTING_TYPE_ID = "semantic.security-listing-link"
FINANCIAL_FACT_TYPE_ID = "semantic.financial-fact"
CORPORATE_ACTION_TYPE_ID = "semantic.corporate-action"
UNIVERSE_MEMBERSHIP_TYPE_ID = "semantic.universe-membership"

IDENTITY_SOURCE_ID = "source.fixture-sec-identity"
FINANCIAL_SOURCE_ID = "source.fixture-sec-companyfacts"
ACTION_SOURCE_ID = "source.fixture-yahoo-actions"
MEMBERSHIP_SOURCE_ID = "source.fixture-nport"


def _module_sha256(filename: str) -> str:
    return hashlib.sha256(Path(__file__).with_name(filename).read_bytes()).hexdigest()


def _model_sha256(model_type: type[BaseModel]) -> str:
    return canonical_sha256(
        {
            "model": f"{model_type.__module__}:{model_type.__name__}",
            "schema": model_type.model_json_schema(mode="validation"),
        }
    )


def _semantic_type(
    semantic_type_id: str,
    domain: DataDomain,
    model_type: type[BaseModel],
) -> SemanticTypeRegistryEntry:
    model_name = model_type.__name__
    schema_sha256 = canonical_sha256(model_type.model_json_schema(mode="validation"))
    return SemanticTypeRegistryEntry(
        semantic_type_id=semantic_type_id,
        version=MEDIUM_VERSION,
        domain=domain,
        schema_version=MEDIUM_VERSION,
        schema_fingerprint_sha256=schema_sha256,
        normalized_model_key=f"{model_type.__module__}:{model_name}",
        input_model_key="truealpha_contracts:ProvenanceNeutralInput",
        repository_key=f"data_engine.mvp_medium_repository:{model_name}",
        projector_key="data_engine.mvp_medium_repository:project_provenance_neutral",
        compatibility_sha256=canonical_sha256({"compatible_schema_versions": []}),
        model_implementation_sha256=_model_sha256(model_type),
        repository_implementation_sha256=_module_sha256("mvp_medium_repository.py"),
        projector_implementation_sha256=_module_sha256("mvp_medium_repository.py"),
    )


def _source(
    source_id: str,
    *,
    domains: tuple[DataDomain, ...],
    semantic_type_ids: tuple[str, ...],
    implementation_sha256: str,
) -> SourceRegistryEntry:
    suffix = source_id.removeprefix("source.").replace("-", "_")
    return SourceRegistryEntry(
        source_id=source_id,
        version=MEDIUM_VERSION,
        adapter_id=f"data_engine.d2:{suffix}_adapter",
        adapter_version=MEDIUM_VERSION,
        normalizer_id=f"data_engine.d2:{suffix}_normalizer",
        normalizer_version=MEDIUM_VERSION,
        supported_domains=domains,
        supported_type_ids=semantic_type_ids,
        configuration_schema_sha256=canonical_sha256({"source_id": source_id, "network": False, "credentials": False}),
        mapping_schema_sha256=canonical_sha256({"source_id": source_id, "semantic_type_ids": semantic_type_ids}),
        adapter_implementation_sha256=implementation_sha256,
        normalizer_implementation_sha256=implementation_sha256,
    )


def build_medium_registry(
    price_registry: RegistrySnapshot,
    *,
    source_implementation_sha256: str,
) -> tuple[RegistrySnapshot, RegistryHistory]:
    """Extend the exact D1+price registry without changing inherited entries."""

    if len(source_implementation_sha256) != 64 or set(source_implementation_sha256) - set("0123456789abcdef"):
        raise ValueError("source implementation hash must be a lowercase SHA-256")
    filing_registry = build_filing_registry()
    if price_registry.parent_snapshot_id != filing_registry.registry_snapshot_id:
        raise ValueError("D2 registry must extend the exact accepted D1 registry")
    inherited_price = next(
        (
            entry
            for entry in price_registry.semantic_types
            if entry.semantic_type_id == "semantic.market-price" and entry.version == MEDIUM_VERSION
        ),
        None,
    )
    if inherited_price is None:
        raise ValueError("D2 parent registry does not contain the E0 market-price type")
    if inherited_price.schema_fingerprint_sha256 != canonical_sha256(
        MarketPricePayload.model_json_schema(mode="validation")
    ):
        raise ValueError("shared market-price model drifted from the accepted E0 schema")

    semantic_types = (
        _semantic_type(ISSUER_SECURITY_TYPE_ID, DataDomain.INSTRUMENTS, IssuerSecurityLink),
        _semantic_type(SECURITY_LISTING_TYPE_ID, DataDomain.INSTRUMENTS, SecurityListingLink),
        _semantic_type(FINANCIAL_FACT_TYPE_ID, DataDomain.FINANCIAL_FACTS, FinancialFact),
        _semantic_type(CORPORATE_ACTION_TYPE_ID, DataDomain.CORPORATE_ACTIONS, CorporateAction),
        _semantic_type(UNIVERSE_MEMBERSHIP_TYPE_ID, DataDomain.UNIVERSE, UniverseMembership),
    )
    sources = (
        _source(
            IDENTITY_SOURCE_ID,
            domains=(DataDomain.INSTRUMENTS,),
            semantic_type_ids=(ISSUER_SECURITY_TYPE_ID, SECURITY_LISTING_TYPE_ID),
            implementation_sha256=source_implementation_sha256,
        ),
        _source(
            FINANCIAL_SOURCE_ID,
            domains=(DataDomain.FINANCIAL_FACTS,),
            semantic_type_ids=(FINANCIAL_FACT_TYPE_ID,),
            implementation_sha256=source_implementation_sha256,
        ),
        _source(
            ACTION_SOURCE_ID,
            domains=(DataDomain.CORPORATE_ACTIONS,),
            semantic_type_ids=(CORPORATE_ACTION_TYPE_ID,),
            implementation_sha256=source_implementation_sha256,
        ),
        _source(
            MEMBERSHIP_SOURCE_ID,
            domains=(DataDomain.UNIVERSE,),
            semantic_type_ids=(UNIVERSE_MEMBERSHIP_TYPE_ID,),
            implementation_sha256=source_implementation_sha256,
        ),
    )
    registry = price_registry.extend(
        sources=sources,
        semantic_types=semantic_types,
        required_type_ids=(*price_registry.required_type_ids, *(entry.semantic_type_id for entry in semantic_types)),
    )
    history = RegistryHistory(snapshots=(filing_registry, price_registry, registry))
    return registry, history


__all__ = [
    "ACTION_SOURCE_ID",
    "CORPORATE_ACTION_TYPE_ID",
    "FINANCIAL_FACT_TYPE_ID",
    "FINANCIAL_SOURCE_ID",
    "IDENTITY_SOURCE_ID",
    "ISSUER_SECURITY_TYPE_ID",
    "MEDIUM_VERSION",
    "MEMBERSHIP_SOURCE_ID",
    "SECURITY_LISTING_TYPE_ID",
    "UNIVERSE_MEMBERSHIP_TYPE_ID",
    "build_medium_registry",
]
