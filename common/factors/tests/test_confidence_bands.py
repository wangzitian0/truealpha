"""#1517 A2: one band function. Its thresholds live in `confidence_bands.v1.json`.

The four evaluator cases are the probe of the audit (registry outcome masses sec 90, moomoo 40,
twelve-data 40, yahoo 12; agreement 1). The three sentences of `init.md` give the words for
0.8563, 0.6247 and 0.5971. Before this change one code rule (0.85 and 0.50) gave "medium" for
0.8457 and 0.5971.
"""

from __future__ import annotations

import json
from decimal import Decimal
from importlib import resources

import pytest
from factors.confidence import evaluate_continuous_confidence
from truealpha_contracts.confidence import (
    ContinuousConfidenceInput,
    ContinuousConfidencePolicy,
    SourceConfidenceEvidence,
)
from truealpha_contracts.confidence_bands import CONFIDENCE_BANDS_RESOURCE, confidence_band

_OUTCOME_MASS = {"sec": "90", "moomoo": "40", "twelve-data": "40", "yahoo": "12"}


def _source(lineage: str) -> SourceConfidenceEvidence:
    return SourceConfidenceEvidence(
        provider_id=f"provider:{lineage}",
        origin_group_id=f"lineage:{lineage}",
        independence_weight="1",
        successful_outcome_mass=_OUTCOME_MASS[lineage],
        failed_outcome_mass="0",
        freshness="1",
        sample_conformance="1",
        transport_integrity="1",
        evidence_ids=(f"evidence:{lineage}:v1",),
        reason_codes=("measured",),
    )


def _evaluate(*lineages: str) -> Decimal:
    evaluation = evaluate_continuous_confidence(
        ContinuousConfidencePolicy(),
        ContinuousConfidenceInput(
            case_id=f"cell:{'+'.join(lineages)}",
            sources=tuple(_source(lineage) for lineage in lineages),
            agreement="1",
            semantic_mapping_quality="1",
            lineage_completeness="1",
            required_component_completeness="1",
        ),
    )
    return evaluation.confidence


@pytest.mark.parametrize(
    ("lineages", "rounded", "band"),
    [
        (("sec", "moomoo"), "0.8563", "high"),
        (("yahoo", "twelve-data"), "0.8457", "high"),
        (("sec",), "0.6247", "medium"),
        (("yahoo",), "0.5971", "low"),
    ],
)
def test_the_evaluator_value_gets_the_band_of_the_three_init_sentences(
    lineages: tuple[str, ...], rounded: str, band: str
) -> None:
    confidence = _evaluate(*lineages)
    assert confidence.quantize(Decimal("0.0001")) == Decimal(rounded)
    assert confidence_band(confidence) == band


@pytest.mark.parametrize(
    ("confidence", "band"),
    [
        (Decimal("1"), "high"),
        (Decimal("0.80"), "high"),
        (Decimal("0.7999"), "medium"),
        (Decimal("0.60"), "medium"),
        (Decimal("0.5999"), "low"),
        (Decimal("0"), "low"),
        (None, None),
    ],
)
def test_the_band_changes_at_the_two_thresholds_and_none_has_no_band(
    confidence: Decimal | None, band: str | None
) -> None:
    assert confidence_band(confidence) == band


def test_the_thresholds_are_the_ones_the_json_file_holds() -> None:
    payload = json.loads(resources.files("truealpha_contracts.data").joinpath(CONFIDENCE_BANDS_RESOURCE).read_bytes())
    high_from, medium_from = Decimal(payload["high_from"]), Decimal(payload["medium_from"])
    assert medium_from < high_from <= Decimal(1)
    step = Decimal("0.000001")
    assert confidence_band(high_from) == "high"
    assert confidence_band(high_from - step) == "medium"
    assert confidence_band(medium_from) == "medium"
    assert confidence_band(medium_from - step) == "low"
