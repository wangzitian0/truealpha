"""Producer-computed §8 availability, evidence, and validation status dimensions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from factors.validation_records import validation_status_for
from psycopg import Connection
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus, InputEvidenceStatus

LOW_CONFIDENCE_FLOOR = Decimal("0.50")


@dataclass(frozen=True)
class StatusDimensions:
    availability_status: AvailabilityStatus
    source_evidence_status: InputEvidenceStatus
    factor_validation_status: FactorValidationStatus

    def as_columns(self) -> tuple[str, str, str]:
        return (
            self.availability_status.value,
            self.source_evidence_status.value,
            self.factor_validation_status.value,
        )


def availability_status_for(
    *,
    availability: str,
    freshness: str,
    confidence: Decimal,
    reason_codes: Iterable[str] = (),
) -> AvailabilityStatus:
    reasons = set(reason_codes)
    if availability == "unavailable":
        return AvailabilityStatus.UNAVAILABLE
    if freshness == "stale" or "stale_input" in reasons:
        return AvailabilityStatus.STALE
    if freshness == "unknown" or "unknown_freshness" in reasons:
        return AvailabilityStatus.ERROR
    if confidence < LOW_CONFIDENCE_FLOOR:
        return AvailabilityStatus.LOW_CONFIDENCE
    return AvailabilityStatus.AVAILABLE


def decision_availability_status(
    *, eligible: bool, exclusion_reason: str | None, value_present: bool
) -> AvailabilityStatus:
    if not eligible and exclusion_reason:
        return AvailabilityStatus.EXCLUDED
    if not value_present:
        return AvailabilityStatus.UNAVAILABLE
    return AvailabilityStatus.AVAILABLE


def coverage_status_dimensions(
    *,
    value_present: bool,
    confidence: Decimal,
    fully_evidenced: bool,
) -> tuple[AvailabilityStatus, InputEvidenceStatus, FactorValidationStatus]:
    if not value_present:
        return (AvailabilityStatus.UNAVAILABLE, InputEvidenceStatus.DEGRADED, FactorValidationStatus.NOT_EVALUATED)
    availability = (
        AvailabilityStatus.LOW_CONFIDENCE if confidence < LOW_CONFIDENCE_FLOOR else AvailabilityStatus.AVAILABLE
    )
    evidence = InputEvidenceStatus.VERIFIED if fully_evidenced else InputEvidenceStatus.DEGRADED
    return (availability, evidence, FactorValidationStatus.NOT_EVALUATED)


def resolve_raw_pointers(connection: Connection[Any], observation_ids: Iterable[str]) -> dict[str, int | None]:
    wanted = tuple(dict.fromkeys(observation_ids))
    if not wanted:
        return {}
    rows = connection.execute(
        """
        select o.observation_id, f.id
        from staging.capture_normalized_observations o
        left join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
        left join raw.fetches f on f.id = v.raw_fetch_id
        where o.observation_id = any(%s)
        """,
        (list(wanted),),
    ).fetchall()
    return {str(observation_id): fetch_id for observation_id, fetch_id in rows}


def source_evidence_status_for(
    connection: Connection[Any],
    observation_ids: Iterable[str],
    *,
    financial_payloads: Iterable[Mapping[str, Any]] = (),
    resolved: Mapping[str, int | None] | None = None,
) -> InputEvidenceStatus:
    wanted = tuple(dict.fromkeys(observation_ids))
    if not wanted:
        return InputEvidenceStatus.REJECTED
    if resolved is None:
        resolved = resolve_raw_pointers(connection, wanted)
    if any(resolved.get(observation_id) is None for observation_id in wanted):
        return InputEvidenceStatus.REJECTED
    for payload in financial_payloads:
        if payload.get("headcount") is None:
            continue
        headcount_vintage = (payload.get("vintage") or {}).get("headcount") or {}
        if not headcount_vintage.get("raw"):
            return InputEvidenceStatus.DEGRADED
    return InputEvidenceStatus.VERIFIED


def factor_validation_status_for(definition_ids: Iterable[str]) -> FactorValidationStatus:
    return validation_status_for(definition_ids)


def statuses_without_raw_pointer(
    definition_ids: Iterable[str] = (),
) -> tuple[InputEvidenceStatus, FactorValidationStatus]:
    return InputEvidenceStatus.DEGRADED, factor_validation_status_for(definition_ids)
