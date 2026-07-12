import runpy
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cache
from pathlib import Path
from typing import Any

import dagster as dg
from data_engine.contract_assets import (
    CONTRACT_EVIDENCE_ASSETS,
    UsageWindow,
    compile_registry_asset_specs,
)
from truealpha_contracts.data_quality import DataDomain
from truealpha_contracts.execution import FactorInvocationTemplate, FactorKind
from truealpha_contracts.registries import RegistrySnapshot, SemanticTypeRegistryEntry, SourceRegistryEntry
from truealpha_contracts.universe import SubjectKind
from truealpha_contracts.usage import (
    DataRequirement,
    RequirementLevel,
    StrategyDataQualityReview,
    StrategyUsageAudit,
    UsageFrequencySlice,
)


@cache
def _contract_fixtures() -> dict[str, Any]:
    return runpy.run_path(str(Path(__file__).with_name("test_contract_repository.py")))


def _usage_audit() -> StrategyUsageAudit:
    audit = _contract_fixtures()["_strategy_usage_audit"]()
    assert isinstance(audit, StrategyUsageAudit)
    return audit


def _quality_review() -> StrategyDataQualityReview:
    review = _contract_fixtures()["_strategy_data_quality_review"]()
    assert isinstance(review, StrategyDataQualityReview)
    return review


@dataclass
class _MemoryEvidenceStore:
    calls: list[tuple[str, str]] = field(default_factory=list)

    def put_strategy_usage_audit(self, audit: StrategyUsageAudit) -> bool:
        self.calls.append(("audit", audit.strategy_usage_audit_id))
        return True

    def put_usage_frequency_slice(self, frequency: UsageFrequencySlice) -> bool:
        self.calls.append(("frequency", frequency.usage_frequency_slice_id))
        return True

    def put_strategy_data_quality_review(self, review: StrategyDataQualityReview) -> bool:
        self.calls.append(("review", review.review_id))
        return True


def test_dagster_materializes_complete_audit_before_frequency_and_review() -> None:
    audit = _usage_audit()
    review = _quality_review()
    window = UsageWindow(
        window_start=audit.run_started_at - timedelta(minutes=1),
        window_end=audit.run_completed_at + timedelta(days=1),
    )

    @dg.asset
    def prepared_strategy_usage_audit() -> StrategyUsageAudit:
        return audit

    @dg.asset
    def usage_window() -> UsageWindow:
        return window

    @dg.asset
    def prepared_strategy_data_quality_review() -> StrategyDataQualityReview:
        return review

    upstream = (
        prepared_strategy_usage_audit,
        usage_window,
        prepared_strategy_data_quality_review,
    )
    assets = (*upstream, *CONTRACT_EVIDENCE_ASSETS)
    store = _MemoryEvidenceStore()
    definitions = dg.Definitions(
        assets=list(assets),
        resources={"contract_evidence_store": store},
    )
    dg.Definitions.validate_loadable(definitions)
    graph = definitions.get_repository_def().asset_graph
    audit_key = dg.AssetKey("strategy_usage_audit")
    for child_key in (dg.AssetKey("usage_frequency_slice"), dg.AssetKey("strategy_data_quality_review")):
        parent_keys = {node.key for node in graph.get_parents(graph.get(child_key))}
        assert audit_key in parent_keys

    result = dg.materialize(
        list(assets),
        resources={"contract_evidence_store": store},
    )

    assert result.success
    call_kinds = [kind for kind, _contract_id in store.calls]
    assert call_kinds.count("audit") == call_kinds.count("frequency") == call_kinds.count("review") == 1
    assert call_kinds.index("audit") < call_kinds.index("frequency")
    assert call_kinds.index("audit") < call_kinds.index("review")
    frequency = result.output_for_node("usage_frequency_slice")
    materialized_review = result.output_for_node("strategy_data_quality_review")
    assert isinstance(frequency, UsageFrequencySlice)
    assert frequency.strategy_usage_audit_ids == (audit.strategy_usage_audit_id,)
    assert materialized_review == review


def _requirement(semantic_type_id: str, *, seed: str) -> DataRequirement:
    return DataRequirement(
        capture_requirement_id="capture-requirement:" + seed * 64,
        semantic_type_id=semantic_type_id,
        domain=DataDomain.FINANCIAL_FACTS,
        metric="probe_signal" if "probe" in semantic_type_id else "revenue",
        subject_kinds=frozenset({SubjectKind.ISSUER}),
        level=RequirementLevel.REQUIRED,
        lookback=timedelta(days=30),
        valid_period_rule_id="fiscal-period:annual",
        maximum_age=timedelta(days=90),
        cadence=timedelta(days=1),
    )


def _template(requirement: DataRequirement, *, factor_id: str, seed: str) -> FactorInvocationTemplate:
    return FactorInvocationTemplate(
        factor_id=factor_id,
        factor_version="1.0.0",
        factor_implementation_sha256=seed * 64,
        factor_kind=FactorKind.BASE,
        parameter_model_key="contracts:NoParameters",
        parameter_schema_sha256="a" * 64,
        canonical_parameters_sha256="b" * 64,
        data_requirement_ids=(requirement.requirement_id,),
    )


def _additive_registry(baseline: RegistrySnapshot) -> RegistrySnapshot:
    probe_type = SemanticTypeRegistryEntry(
        semantic_type_id="semantic.probe-signal",
        version="1.0.0",
        domain=DataDomain.FINANCIAL_FACTS,
        schema_version="1.0.0",
        schema_fingerprint_sha256="1" * 64,
        normalized_model_key="contracts:ProbeSignal",
        input_model_key="factors:ProbeSignalInput",
        repository_key="repositories:ProbeSignal",
        projector_key="projectors:ProbeSignal",
        compatibility_sha256="2" * 64,
        model_implementation_sha256="3" * 64,
        repository_implementation_sha256="4" * 64,
        projector_implementation_sha256="5" * 64,
    )
    probe_source = SourceRegistryEntry(
        source_id="source.probe-fixture",
        version="1.0.0",
        adapter_id="adapter.probe_fixture",
        adapter_version="1.0.0",
        normalizer_id="normalizer.probe_fixture",
        normalizer_version="1.0.0",
        supported_domains=(DataDomain.FINANCIAL_FACTS,),
        supported_type_ids=(baseline.semantic_types[0].semantic_type_id, probe_type.semantic_type_id),
        configuration_schema_sha256="6" * 64,
        mapping_schema_sha256="7" * 64,
        adapter_implementation_sha256="8" * 64,
        normalizer_implementation_sha256="9" * 64,
    )
    return RegistrySnapshot(
        sources=(*baseline.sources, probe_source),
        semantic_types=(*baseline.semantic_types, probe_type),
        identifier_types=baseline.identifier_types,
        required_type_ids=(*baseline.required_type_ids, probe_type.semantic_type_id),
        required_identifier_type_ids=baseline.required_identifier_type_ids,
    )


def _spec_map(specs: tuple[dg.AssetSpec, ...]) -> dict[str, tuple[str, ...]]:
    return {
        spec.key.to_user_string(): tuple(sorted(dependency.asset_key.to_user_string() for dependency in spec.deps))
        for spec in specs
    }


def test_registry_composition_adds_source_type_and_probe_factor_without_switches() -> None:
    baseline = _contract_fixtures()["_registry"]()
    assert isinstance(baseline, RegistrySnapshot)
    base_requirement = _requirement(baseline.semantic_types[0].semantic_type_id, seed="c")
    base_template = _template(base_requirement, factor_id="factor.base", seed="d")
    baseline_specs = compile_registry_asset_specs(
        registry=baseline,
        requirements=(base_requirement,),
        factor_templates=(base_template,),
    )

    additive = _additive_registry(baseline)
    probe_requirement = _requirement("semantic.probe-signal", seed="e")
    probe_template = _template(probe_requirement, factor_id="factor.additive-probe", seed="f")
    additive_specs = compile_registry_asset_specs(
        registry=additive,
        requirements=(base_requirement, probe_requirement),
        factor_templates=(base_template, probe_template),
    )
    dg.Definitions.validate_loadable(dg.Definitions(assets=list(additive_specs)))

    before = _spec_map(baseline_specs)
    after = _spec_map(additive_specs)
    base_factor_key = f"factor/{base_template.factor_template_id}"
    assert after[base_factor_key] == before[base_factor_key]
    assert all(key in after for key in before if key != "capture_manifest")
    assert any(key.startswith("capture/source.probe-fixture/") for key in after)
    assert any(key.startswith("normalize/semantic.financial-fact/") and "source.probe-fixture" in key for key in after)
    assert any(key.startswith("snapshot/semantic.probe-signal/") for key in after)
    assert f"factor/{probe_template.factor_template_id}" in after
