"""Factor availability, input evidence, and validation status enums."""

from __future__ import annotations

from enum import StrEnum


class InputEvidenceStatus(StrEnum):
    VERIFIED = "verified"
    DEGRADED = "degraded"
    REJECTED = "rejected"


class AvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    EXCLUDED = "excluded"
    LOW_CONFIDENCE = "low_confidence"
    ERROR = "error"


class FactorValidationStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NOT_EVALUATED = "not_evaluated"


__all__ = [
    "AvailabilityStatus",
    "FactorValidationStatus",
    "InputEvidenceStatus",
]
