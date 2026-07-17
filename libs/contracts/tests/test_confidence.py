from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from truealpha_contracts.confidence import (
    ContinuousConfidenceInput,
    ContinuousConfidencePolicy,
    SourceConfidenceEvidence,
)


def _source(*, weight: str = "1") -> SourceConfidenceEvidence:
    return SourceConfidenceEvidence(
        provider_id="provider:primary",
        origin_group_id="origin:primary",
        independence_weight=weight,
        successful_outcome_mass="8",
        failed_outcome_mass="2",
        freshness="1",
        sample_conformance="1",
        transport_integrity="1",
        evidence_ids=("evidence:primary:v1",),
        reason_codes=("measured",),
    )


def test_policy_is_content_addressed_and_rejects_parameter_drift() -> None:
    first = ContinuousConfidencePolicy()
    repeated = ContinuousConfidencePolicy()
    changed = ContinuousConfidencePolicy(agreement_exponent="0.30", semantic_mapping_exponent="0.30")

    assert first == repeated
    assert first.policy_id == "confidence-policy:" + first.content_sha256
    assert first.policy_id != changed.policy_id
    with pytest.raises(ValidationError, match="sum exactly to one"):
        ContinuousConfidencePolicy(agreement_exponent="0.30")


def test_confidence_contracts_reject_binary_float_inputs() -> None:
    binary_float: Any = 0.8
    with pytest.raises(ValidationError, match="binary float"):
        ContinuousConfidencePolicy(unobserved_reliability_ceiling=binary_float)
    with pytest.raises(ValidationError, match="binary float"):
        SourceConfidenceEvidence(
            **{
                **_source().model_dump(
                    mode="python",
                    exclude={"source_evidence_id", "content_sha256", "freshness"},
                ),
                "freshness": binary_float,
            }
        )


def test_input_is_order_independent_and_one_origin_has_one_weight() -> None:
    primary = _source()
    mirror = SourceConfidenceEvidence(
        **{
            **primary.model_dump(
                mode="python",
                exclude={"source_evidence_id", "content_sha256", "provider_id", "evidence_ids"},
            ),
            "provider_id": "provider:mirror",
            "evidence_ids": ("evidence:mirror:v1",),
        }
    )
    fields = {
        "case_id": "case:same-origin",
        "agreement": Decimal("1"),
        "semantic_mapping_quality": Decimal("1"),
        "lineage_completeness": Decimal("1"),
        "required_component_completeness": Decimal("1"),
    }
    first = ContinuousConfidenceInput(sources=(primary, mirror), **fields)
    repeated = ContinuousConfidenceInput(sources=(mirror, primary), **fields)
    assert first == repeated

    wrong_weight = SourceConfidenceEvidence(
        **{
            **mirror.model_dump(
                mode="python",
                exclude={"source_evidence_id", "content_sha256", "independence_weight"},
            ),
            "independence_weight": "0.5",
        }
    )
    with pytest.raises(ValidationError, match="one independence weight"):
        ContinuousConfidenceInput(sources=(primary, wrong_weight), **fields)
