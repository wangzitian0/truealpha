from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus, InputEvidenceStatus


def test_execution_status_enums_preserve_wire_values() -> None:
    assert tuple(s.value for s in AvailabilityStatus) == (
        "available",
        "unavailable",
        "stale",
        "excluded",
        "low_confidence",
        "not_applicable",
        "error",
    )
    assert tuple(s.value for s in FactorValidationStatus) == ("accepted", "rejected", "not_evaluated")
    assert tuple(s.value for s in InputEvidenceStatus) == ("verified", "degraded", "rejected")
