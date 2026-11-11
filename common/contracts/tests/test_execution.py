from __future__ import annotations

from truealpha_contracts.execution import (
    AvailabilityStatus,
    FactorValidationStatus,
    InputEvidenceStatus,
)


def test_execution_status_enums_preserve_mart_and_consumer_wire_values() -> None:
    assert tuple(s.value for s in AvailabilityStatus) == (
        "available",
        "unavailable",
        "stale",
        "excluded",
        "low_confidence",
        "error",
    )
    assert tuple(s.value for s in FactorValidationStatus) == (
        "accepted",
        "rejected",
        "not_evaluated",
    )
    assert tuple(s.value for s in InputEvidenceStatus) == (
        "verified",
        "degraded",
        "rejected",
    )
