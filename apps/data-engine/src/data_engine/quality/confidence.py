"""Sample-bound continuous-confidence calibration for DataHub evidence."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import ROUND_HALF_EVEN, Context, Decimal, DivisionByZero, InvalidOperation, Overflow, localcontext
from pathlib import Path
from typing import Literal

from truealpha_contracts.confidence import (
    ConfidenceCalibrationReport,
    ConfidenceCalibrationScenario,
    ContinuousConfidenceInput,
    ContinuousConfidencePolicy,
    SourceConfidenceEvidence,
    evaluate_continuous_confidence,
)

_RECONCILIATION_REPORT = "twelve_data_reconciliation_20260714.json"
_RECONCILIATION_MANIFEST = "independent_reconciliation.v1.json"
_OBSERVED_FIELDS = ("close", "high", "low", "open", "volume")
_REQUIRED_PRICE_COMPONENT_COUNT = 7
_AGREEMENT_DECIMAL_PLACES = 12
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _calibration_context() -> Context:
    return Context(
        prec=50,
        rounding=ROUND_HALF_EVEN,
        Emin=-999999,
        Emax=999999,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain one JSON object")
    return value


def _sample_artifact_id(path: Path) -> str:
    return f"sample-artifact-sha256:{_sha256(path)}"


def _derive_price_reconciliation_anchor(
    sample_root: Path,
) -> tuple[Decimal, Decimal, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Derive agreement and evidence identities from checked-in artifacts."""

    prices = sample_root / "prices"
    report_path = prices / _RECONCILIATION_REPORT
    manifest_path = prices / _RECONCILIATION_MANIFEST
    report = _load_json(report_path)
    manifest = _load_json(manifest_path)
    if (
        report.get("schema") != "truealpha.price-source-reconciliation@v1"
        or report.get("status") != "observed_full_yahoo_window"
        or report.get("primary_source") != "yahoo_chart"
        or report.get("independent_source") != "twelve_data"
    ):
        raise ValueError("price reconciliation report has an unsupported identity or status")

    primary_ids = {_sample_artifact_id(report_path), _sample_artifact_id(manifest_path)}
    resolved_sample_root = sample_root.resolve()
    primary_artifacts = manifest.get("primary_artifacts")
    if not isinstance(primary_artifacts, list) or len(primary_artifacts) != 4:
        raise ValueError("price reconciliation manifest must bind four primary artifacts")
    primary_symbols: set[str] = set()
    primary_paths: set[str] = set()
    primary_hashes: set[str] = set()
    for artifact in primary_artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("primary artifact entries must be objects")
        symbol = artifact.get("symbol")
        relative_path = artifact.get("path")
        expected_sha256 = artifact.get("sha256")
        if (
            not isinstance(symbol, str)
            or not isinstance(relative_path, str)
            or not isinstance(expected_sha256, str)
            or _SHA256.fullmatch(expected_sha256) is None
        ):
            raise ValueError("primary artifacts must declare symbol, path, and sha256")
        if symbol in primary_symbols or relative_path in primary_paths or expected_sha256 in primary_hashes:
            raise ValueError("primary sample symbol, path, and sha256 bindings must be one-to-one")
        if not Path(relative_path).name.startswith(f"{symbol}_prices_"):
            raise ValueError("primary sample filename must match its declared symbol")
        primary_symbols.add(symbol)
        primary_paths.add(relative_path)
        primary_hashes.add(expected_sha256)
        artifact_path = (sample_root / relative_path).resolve()
        if not artifact_path.is_relative_to(resolved_sample_root):
            raise ValueError(f"primary sample path escapes the sample root: {relative_path}")
        actual_sha256 = _sha256(artifact_path)
        if actual_sha256 != expected_sha256:
            raise ValueError(f"primary sample hash mismatch: {relative_path}")
        primary_ids.add(f"sample-artifact-sha256:{actual_sha256}")

    observations = report.get("observations")
    if not isinstance(observations, list) or len(observations) != 4:
        raise ValueError("price reconciliation report must contain four observations")
    independent_ids = {_sample_artifact_id(report_path)}
    compared = 0
    conforming = 0
    subject_ids: set[str] = set()
    response_hashes: set[str] = set()
    for observation in observations:
        if not isinstance(observation, dict):
            raise ValueError("price reconciliation observations must be objects")
        symbol = observation.get("symbol")
        response_sha256 = observation.get("twelve_data_response_sha256")
        common_dates = observation.get("common_dates")
        field_stats = observation.get("field_stats")
        if (
            not isinstance(symbol, str)
            or not isinstance(response_sha256, str)
            or _SHA256.fullmatch(response_sha256) is None
            or not isinstance(common_dates, int)
            or common_dates <= 0
            or not isinstance(field_stats, dict)
            or tuple(sorted(field_stats)) != _OBSERVED_FIELDS
        ):
            raise ValueError("price reconciliation observation is incomplete")
        if response_sha256 in response_hashes:
            raise ValueError("each reconciliation subject must bind one unique provider response hash")
        response_hashes.add(response_sha256)
        subject_ids.add(f"ticker:{symbol}")
        independent_ids.add(f"provider-response-sha256:{response_sha256}")
        for field in _OBSERVED_FIELDS:
            stats = field_stats[field]
            if not isinstance(stats, dict):
                raise ValueError("field reconciliation statistics must be objects")
            count = stats.get("count")
            within_tolerance = stats.get("within_tolerance")
            if count != common_dates or not isinstance(within_tolerance, int) or not 0 <= within_tolerance <= count:
                raise ValueError("field reconciliation statistics have an invalid denominator")
            compared += count
            conforming += within_tolerance
    if len(subject_ids) != len(observations):
        raise ValueError("price reconciliation subjects must be unique")
    if {subject.removeprefix("ticker:") for subject in subject_ids} != primary_symbols:
        raise ValueError("primary sample symbols must exactly match reconciliation observations")

    with localcontext(_calibration_context()):
        agreement = (Decimal(conforming) / Decimal(compared)).quantize(Decimal(1).scaleb(-_AGREEMENT_DECIMAL_PLACES))
        completeness = Decimal(len(_OBSERVED_FIELDS)) / Decimal(_REQUIRED_PRICE_COMPONENT_COUNT)
    return (
        agreement,
        completeness,
        tuple(sorted(primary_ids)),
        tuple(sorted(independent_ids)),
        tuple(sorted(subject_ids)),
    )


def _source(
    case_id: str,
    provider_id: str,
    origin_group_id: str,
    *,
    independence_weight: str = "1",
    successful_outcome_mass: str = "1000",
    failed_outcome_mass: str = "0",
    freshness: str = "1",
    sample_conformance: str = "1",
    transport_integrity: str = "1",
    evidence_ids: tuple[str, ...] | None = None,
    reason_codes: tuple[str, ...] = ("sample-evidence.measured",),
) -> SourceConfidenceEvidence:
    return SourceConfidenceEvidence(
        provider_id=provider_id,
        origin_group_id=origin_group_id,
        independence_weight=Decimal(independence_weight),
        successful_outcome_mass=Decimal(successful_outcome_mass),
        failed_outcome_mass=Decimal(failed_outcome_mass),
        freshness=Decimal(freshness),
        sample_conformance=Decimal(sample_conformance),
        transport_integrity=Decimal(transport_integrity),
        evidence_ids=evidence_ids or (f"sample-evidence:{case_id}:{provider_id}",),
        reason_codes=reason_codes,
    )


def _evaluate_case(
    policy: ContinuousConfidencePolicy,
    case_id: str,
    sources: tuple[SourceConfidenceEvidence, ...],
    *,
    agreement: str = "1",
    semantic_mapping_quality: str = "1",
    lineage_completeness: str = "1",
    required_component_completeness: str = "1",
) -> ContinuousConfidenceInput:
    return ContinuousConfidenceInput(
        case_id=case_id,
        sources=sources,
        agreement=Decimal(agreement),
        semantic_mapping_quality=Decimal(semantic_mapping_quality),
        lineage_completeness=Decimal(lineage_completeness),
        required_component_completeness=Decimal(required_component_completeness),
    )


def build_topt_confidence_sensitivity_report(sample_root: Path | None = None) -> ConfidenceCalibrationReport:
    """Build the reviewable v0.1 report without claiming full-TOPT empirical calibration."""

    policy = ContinuousConfidencePolicy()
    scenarios: list[ConfidenceCalibrationScenario] = []

    def add(
        scenario_id: str,
        expected_effect: str,
        sources: tuple[SourceConfidenceEvidence, ...],
        *,
        evidence_class: Literal["sensitivity", "empirical_anchor"] = "sensitivity",
        agreement: str = "1",
        semantic_mapping_quality: str = "1",
        lineage_completeness: str = "1",
        required_component_completeness: str = "1",
    ) -> None:
        confidence_input = _evaluate_case(
            policy,
            scenario_id,
            sources,
            agreement=agreement,
            semantic_mapping_quality=semantic_mapping_quality,
            lineage_completeness=lineage_completeness,
            required_component_completeness=required_component_completeness,
        )
        scenarios.append(
            ConfidenceCalibrationScenario(
                scenario_id=scenario_id,
                evidence_class=evidence_class,
                expected_effect=expected_effect,
                input=confidence_input,
                evaluation=evaluate_continuous_confidence(policy, confidence_input),
            )
        )

    add(
        "topt.single-independent-source",
        "One near-perfect origin remains capped near 63 support points.",
        (_source("single", "provider:primary", "origin:primary"),),
    )
    add(
        "topt.two-independent-agreeing",
        "A second independent agreeing origin raises support without reaching certainty.",
        (
            _source("two", "provider:primary", "origin:primary"),
            _source("two", "provider:secondary", "origin:secondary"),
        ),
    )
    add(
        "topt.three-independent-agreeing",
        "Three independent agreeing origins approach but do not equal 100.",
        (
            _source("three", "provider:primary", "origin:primary"),
            _source("three", "provider:secondary", "origin:secondary"),
            _source("three", "provider:tertiary", "origin:tertiary"),
        ),
    )
    add(
        "topt.same-origin-duplicate",
        "A mirror of the primary origin contributes no second unit of support.",
        (
            _source("same-origin", "provider:primary", "origin:primary"),
            _source("same-origin", "provider:mirror", "origin:primary"),
        ),
    )
    add(
        "topt.stale-source",
        "Cadence-relative freshness decay reduces source evidence continuously.",
        (_source("stale", "provider:primary", "origin:primary", freshness="0.5"),),
    )
    add(
        "topt.semantic-mismatch",
        "Ambiguous mapping or definition drift lowers the semantic dimension.",
        (_source("semantic", "provider:primary", "origin:primary"),),
        semantic_mapping_quality="0.5",
    )
    add(
        "topt.partial-lineage",
        "Missing provenance edges lower confidence without erasing the observation.",
        (_source("lineage", "provider:primary", "origin:primary"),),
        lineage_completeness="0.5",
    )
    add(
        "topt.missing-components",
        "An incomplete demanded record is penalized instead of removed from the denominator.",
        (_source("completeness", "provider:primary", "origin:primary"),),
        required_component_completeness="0.5",
    )
    add(
        "topt.cross-source-conflict",
        "Independent support cannot hide material cross-source disagreement.",
        (
            _source("conflict", "provider:primary", "origin:primary"),
            _source("conflict", "provider:secondary", "origin:secondary"),
        ),
        agreement="0.2",
    )

    resolved_sample_root = sample_root or Path(__file__).resolve().parents[3] / "samples"
    agreement, completeness, yahoo_evidence_ids, twelve_evidence_ids, empirical_subject_ids = (
        _derive_price_reconciliation_anchor(resolved_sample_root)
    )
    empirical_case = "topt.yahoo-twelve-data-four-symbol-anchor"
    add(
        empirical_case,
        "Four symbols anchor agreement, while missing adjusted close and actions keep the result provisional.",
        (
            _source(
                "empirical",
                "provider:yahoo-chart",
                "origin:yahoo-chart",
                successful_outcome_mass="0",
                evidence_ids=yahoo_evidence_ids,
                reason_codes=("sample.raw-bytes-retained", "sample.sha256-verified"),
            ),
            _source(
                "empirical",
                "provider:twelve-data",
                "origin:twelve-data",
                successful_outcome_mass="0",
                transport_integrity="0",
                evidence_ids=twelve_evidence_ids,
                reason_codes=("sample.aggregate-only", "sample.raw-response-bytes-missing"),
            ),
        ),
        evidence_class="empirical_anchor",
        agreement=str(agreement),
        required_component_completeness=str(completeness),
    )

    return ConfidenceCalibrationReport(
        policy=policy,
        denominator_id="universe:topt-us-2026-03-31",
        denominator_size=20,
        empirically_observed_subject_ids=empirical_subject_ids,
        scenarios=tuple(scenarios),
        limitations=(
            "Independent Yahoo/Twelve Data overlap covers four symbols, not the complete TOPT denominator.",
            "The report is sensitivity evidence and does not freeze a Production threshold.",
            "Adjusted close and corporate-action reconciliation are absent from the empirical anchor.",
            "Twelve Data raw response bytes are absent, so that origin is lineage-only and contributes zero support.",
            "Full TOPT calibration must retain all twenty issuers and report missing second-source evidence.",
        ),
    )
