"""Sealed-holdout verdicts per factor definition (init.md §8 `factor_validation_status`, #65)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from truealpha_contracts.execution import FactorValidationStatus


@dataclass(frozen=True)
class ValidationRecord:
    definition_id: str
    holdout_record_id: str
    status: FactorValidationStatus
    recorded_on: str


VALIDATION_RECORDS: tuple[ValidationRecord, ...] = ()


def validation_status_for(definition_ids: Iterable[str]) -> FactorValidationStatus:
    wanted = tuple(dict.fromkeys(definition_ids))
    if not wanted:
        return FactorValidationStatus.NOT_EVALUATED
    by_definition = {record.definition_id: record.status for record in VALIDATION_RECORDS}
    verdicts = [by_definition.get(definition_id) for definition_id in wanted]
    if any(verdict is FactorValidationStatus.REJECTED for verdict in verdicts):
        return FactorValidationStatus.REJECTED
    if all(verdict is FactorValidationStatus.ACCEPTED for verdict in verdicts):
        return FactorValidationStatus.ACCEPTED
    return FactorValidationStatus.NOT_EVALUATED
