"""#747 / init.md §8: the three status dimensions are derived by the producer, never guessed."""

from __future__ import annotations

from decimal import Decimal

import pytest
from data_engine.datahub.production_topt.status_dimensions import (
    LOW_CONFIDENCE_FLOOR,
    availability_status_for,
    decision_availability_status,
    source_evidence_status_for,
)
from factors import validation_records
from factors.validation_records import ValidationRecord, validation_status_for
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus, InputEvidenceStatus


@pytest.mark.parametrize(
    ("availability", "freshness", "confidence", "reasons", "expected"),
    [
        ("available", "fresh", Decimal("0.9"), (), AvailabilityStatus.AVAILABLE),
        ("unavailable", "fresh", Decimal("0.9"), ("missing_headcount",), AvailabilityStatus.UNAVAILABLE),
        # unavailable wins over stale: a missing number has no age
        ("unavailable", "stale", Decimal("0.9"), ("missing_headcount",), AvailabilityStatus.UNAVAILABLE),
        ("available", "stale", Decimal("1.0"), (), AvailabilityStatus.STALE),
        ("available", "fresh", Decimal("1.0"), ("stale_input",), AvailabilityStatus.STALE),
        ("available", "unknown", Decimal("1.0"), (), AvailabilityStatus.ERROR),
        ("available", "fresh", LOW_CONFIDENCE_FLOOR - Decimal("0.01"), (), AvailabilityStatus.LOW_CONFIDENCE),
        ("available", "fresh", LOW_CONFIDENCE_FLOOR, (), AvailabilityStatus.AVAILABLE),
    ],
)
def test_availability_status_is_ordered_unavailable_stale_error_low_confidence(
    availability, freshness, confidence, reasons, expected
) -> None:
    assert (
        availability_status_for(
            availability=availability, freshness=freshness, confidence=confidence, reason_codes=reasons
        )
        is expected
    )


def test_a_strategy_decision_is_excluded_by_its_own_rule_before_anything_else() -> None:
    assert (
        decision_availability_status(eligible=False, exclusion_reason="financial_branch_rejected", value_present=True)
        is AvailabilityStatus.EXCLUDED
    )
    assert (
        decision_availability_status(eligible=False, exclusion_reason=None, value_present=False)
        is AvailabilityStatus.UNAVAILABLE
    )
    assert (
        decision_availability_status(eligible=True, exclusion_reason=None, value_present=True)
        is AvailabilityStatus.AVAILABLE
    )


def test_validation_is_not_evaluated_until_a_sealed_record_exists(monkeypatch) -> None:
    assert validation_status_for(("gppe-definition:" + "a" * 64,)) is FactorValidationStatus.NOT_EVALUATED
    assert validation_status_for(()) is FactorValidationStatus.NOT_EVALUATED
    accepted = ValidationRecord(
        "gppe-definition:" + "a" * 64, "holdout:1", FactorValidationStatus.ACCEPTED, "2026-09-08"
    )
    rejected = ValidationRecord(
        "three-tier-definition:" + "b" * 64, "holdout:2", FactorValidationStatus.REJECTED, "2026-09-08"
    )
    monkeypatch.setattr(validation_records, "VALIDATION_RECORDS", (accepted,))
    assert validation_status_for((accepted.definition_id,)) is FactorValidationStatus.ACCEPTED
    # a composite with one unevaluated definition was never held out as a whole
    assert (
        validation_status_for((accepted.definition_id, rejected.definition_id)) is FactorValidationStatus.NOT_EVALUATED
    )
    monkeypatch.setattr(validation_records, "VALIDATION_RECORDS", (accepted, rejected))
    assert validation_status_for((accepted.definition_id, rejected.definition_id)) is FactorValidationStatus.REJECTED


class _Conn:
    """A connection whose observation → vintage → fetch chain is scripted."""

    def __init__(self, resolved: dict[str, int | None]):
        self.resolved = resolved

    def execute(self, sql, params):
        (ids,) = params

        class _R:
            def __init__(inner, rows):
                inner.rows = rows

            def fetchall(inner):
                return inner.rows

        return _R([(oid, self.resolved[oid]) for oid in ids if oid in self.resolved])


def test_source_evidence_is_verified_only_when_every_pointer_resolves_and_inputs_are_evidenced() -> None:
    conn = _Conn({"obs:1": 11, "obs:2": 12})
    assert source_evidence_status_for(conn, ("obs:1", "obs:2")) is InputEvidenceStatus.VERIFIED
    # a pointer that does not resolve is rejected, not degraded
    assert (
        source_evidence_status_for(_Conn({"obs:1": 11, "obs:2": None}), ("obs:1", "obs:2"))
        is InputEvidenceStatus.REJECTED
    )
    # an observation the run cannot find at all is rejected
    assert source_evidence_status_for(_Conn({"obs:1": 11}), ("obs:1", "obs:9")) is InputEvidenceStatus.REJECTED
    assert source_evidence_status_for(conn, ()) is InputEvidenceStatus.REJECTED
    # a headcount asserted without evidence on the row is degraded; with a raw pointer it is verified
    seed = {"headcount": "1000", "vintage": {}}
    assert source_evidence_status_for(conn, ("obs:1",), financial_payloads=[seed]) is InputEvidenceStatus.DEGRADED
    cited = {"headcount": "1000", "vintage": {"headcount": {"raw": "raw.fetches:18752", "accession": "x"}}}
    assert source_evidence_status_for(conn, ("obs:1",), financial_payloads=[cited]) is InputEvidenceStatus.VERIFIED
    none = {"headcount": None, "vintage": {}}
    assert source_evidence_status_for(conn, ("obs:1",), financial_payloads=[none]) is InputEvidenceStatus.VERIFIED
