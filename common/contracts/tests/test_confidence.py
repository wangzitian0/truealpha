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

pytestmark = pytest.mark.contract


def _ev(**ch: Any) -> SourceConfidenceEvidence:
    return SourceConfidenceEvidence(
        **{
            "provider_id": "provider:primary",
            "origin_group_id": "origin:primary",
            "independence_weight": "1",
            "successful_outcome_mass": "8",
            "failed_outcome_mass": "2",
            "freshness": "1",
            "sample_conformance": "1",
            "transport_integrity": "1",
            "evidence_ids": ("evidence:primary:v1",),
            "reason_codes": ("measured",),
            **ch,
        }
    )


def test_policy_float_non_finite_and_input_order() -> None:
    p1, p2 = ContinuousConfidencePolicy(), ContinuousConfidencePolicy()
    c = ContinuousConfidencePolicy(agreement_exponent="0.30", semantic_mapping_exponent="0.30")
    assert p1 == p2 and p1.policy_id == "confidence-policy:" + p1.content_sha256 and p1.policy_id != c.policy_id
    with pytest.raises(ValidationError, match="sum exactly to one"):
        ContinuousConfidencePolicy(agreement_exponent="0.30")
    bf: Any = 0.8
    for fn in (lambda: ContinuousConfidencePolicy(unobserved_reliability_ceiling=bf), lambda: _ev(freshness=bf)):
        with pytest.raises(ValidationError, match="binary float"):
            fn()
    for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        for fn in (
            lambda: ContinuousConfidencePolicy(reliability_prior_success=bad),
            lambda: _ev(successful_outcome_mass=bad),
        ):
            with pytest.raises(ValidationError, match="non-finite Decimal"):
                fn()
    p, m = _ev(), _ev(provider_id="provider:mirror", evidence_ids=("evidence:mirror:v1",))
    f = {
        "case_id": "case:same-origin",
        "agreement": Decimal("1"),
        "semantic_mapping_quality": Decimal("1"),
        "lineage_completeness": Decimal("1"),
        "required_component_completeness": Decimal("1"),
    }
    assert ContinuousConfidenceInput(sources=(p, m), **f) == ContinuousConfidenceInput(sources=(m, p), **f)
    with pytest.raises(ValidationError, match="one independence weight"):
        ContinuousConfidenceInput(
            sources=(
                p,
                _ev(provider_id="provider:mirror", evidence_ids=("evidence:mirror:v1",), independence_weight="0.5"),
            ),
            **f,
        )
    wp = _ev(reliability_prior=Decimal("0.96"))
    assert p.reliability_prior is None and wp.reliability_prior == Decimal("0.96")
    assert wp.source_evidence_id != p.source_evidence_id
    for iv in (-Decimal("0.01"), Decimal("1.01"), 0.96):
        with pytest.raises(ValidationError):
            _ev(reliability_prior=iv)
