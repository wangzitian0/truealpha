"""Sealed-holdout verdicts per factor definition (init.md §8 `factor_validation_status`, #65).

The registry is code, append-only and content-addressed by the definition it judges: a
record names the exact `definition_id` (a `gppe-definition:<sha>` / `three-tier-definition:<sha>`
/ `large_model_value_v0:<sha>` identity — the strategy's `strategy_id` plus its definition sha, underscores as the contract spells it), the holdout record that produced the verdict, and
the verdict. A definition with no record is `not_evaluated` — the honest default that #747
makes visible on every row instead of implying acceptance by silence.

Adding a verdict is a PR that appends one `ValidationRecord`; nothing here is ever edited.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from truealpha_contracts.execution import FactorValidationStatus


@dataclass(frozen=True)
class ValidationRecord:
    definition_id: str
    holdout_record_id: str
    status: FactorValidationStatus
    recorded_on: str  # ISO date the sealed holdout was run


# Append-only. Empty on 2026-09-08: no sealed holdout has been executed (#65 open).
VALIDATION_RECORDS: tuple[ValidationRecord, ...] = ()


def validation_status_for(definition_ids: Iterable[str]) -> FactorValidationStatus:
    """The verdict for a row computed from these definitions, fail-closed.

    `rejected` if any definition is rejected; `accepted` only when every definition has an
    accepted record; otherwise `not_evaluated`. A row that mixes an accepted and an
    unevaluated definition is not accepted — the composite was never held out.
    """
    wanted = tuple(dict.fromkeys(definition_ids))
    if not wanted:
        return FactorValidationStatus.NOT_EVALUATED
    by_definition: dict[str, FactorValidationStatus] = {}
    for record in VALIDATION_RECORDS:
        # The latest record for a definition wins; records are appended in order.
        by_definition[record.definition_id] = record.status
    verdicts = [by_definition.get(definition_id) for definition_id in wanted]
    if any(verdict is FactorValidationStatus.REJECTED for verdict in verdicts):
        return FactorValidationStatus.REJECTED
    if all(verdict is FactorValidationStatus.ACCEPTED for verdict in verdicts):
        return FactorValidationStatus.ACCEPTED
    return FactorValidationStatus.NOT_EVALUATED
