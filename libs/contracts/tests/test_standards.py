"""Metric standards (#733): the unit the loop maps onto the wide row."""

from decimal import Decimal

import pytest
from pydantic import ValidationError
from truealpha_contracts.metrics import METRICS
from truealpha_contracts.standards import (
    STANDARDS,
    EvidenceRequirement,
    MetricStandard,
    StandardKind,
    confidence_for,
)


def test_every_standard_names_a_registered_metric_and_carries_the_loop_fields() -> None:
    for name, standard in STANDARDS.items():
        assert name == standard.metric
        assert standard.metric in METRICS
        assert standard.kind in StandardKind
        assert standard.evidence in EvidenceRequirement
        assert standard.max_age_days > 0
        assert standard.evidence_bearing_sources
        assert len(standard.content_sha256) == 64


def test_a_standard_for_an_unregistered_metric_is_refused() -> None:
    with pytest.raises(ValidationError, match="registry does not declare"):
        MetricStandard(
            metric="vibes",
            definition="x",
            acceptance_rule="x",
            kind=StandardKind.HARD,
            evidence=EvidenceRequirement.XBRL_FACT,
            confidence_policy_id="p:v1",
            max_age_days=1,
            evidence_bearing_sources=("sec",),
        )


def test_the_content_hash_changes_when_the_acceptance_rule_changes() -> None:
    base = STANDARDS["employees_total"]
    changed = base.model_copy(update={"acceptance_rule": base.acceptance_rule + " (revised)"})
    assert changed.content_sha256 != base.content_sha256


def test_confidence_is_the_policy_statement_about_the_chooser() -> None:
    assert confidence_for("headcount-confidence:v1", "rule:single-candidate:v1") == Decimal("0.85")
    assert confidence_for("headcount-confidence:v1", "manual-review") == Decimal("0.70")
    with pytest.raises(KeyError, match="no entry for extractor"):
        confidence_for("headcount-confidence:v1", "rule:coin-flip:v1")
