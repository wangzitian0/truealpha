"""The three §8 status dimensions, computed by the producer at persist time (#747).

init.md §8: every factor output carries an `availability_status`, a `source_evidence_status`
for the selected semantic records, and a separate `factor_validation_status` for the factor
version's independent holdout gate. Consumers display all three; they never derive them.
Everything here is derived from what the materialization actually consumed — the result's
own availability/freshness/confidence, the observations it selected, and the definitions it
ran — so the columns add no information to a row's identity (they are not part of the
content hash), they surface what the identity already binds.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from factors.validation_records import validation_status_for
from psycopg import Connection
from truealpha_contracts.execution import AvailabilityStatus, FactorValidationStatus, InputEvidenceStatus

# Below this the number is asserted but the producer does not stand behind it. The same
# floor the research cards use to demote a cell; a policy constant, named so a change is a
# reviewed edit rather than a drifting literal.
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
    """Map a result's availability / freshness / confidence onto the §8 enum.

    Order matters and is deliberate: an unavailable value is unavailable whatever its
    freshness; a stale value is stale even at full confidence; an ungradeable freshness is
    an `error` (the pipeline could not say how old the number is, which is a defect, not a
    property of the issuer); low confidence is reported before `available` so a cell that
    exists but is not stood behind never reads as clean.
    """
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
    """A strategy decision's availability: excluded by the strategy's own rule, unavailable
    when the factor it ranks on is missing, otherwise available."""
    if not eligible and exclusion_reason:
        return AvailabilityStatus.EXCLUDED
    if not value_present:
        return AvailabilityStatus.UNAVAILABLE
    return AvailabilityStatus.AVAILABLE


def resolve_raw_pointers(connection: Connection[Any], observation_ids: Iterable[str]) -> dict[str, int | None]:
    """observation_id → the landed `raw.fetches.id` its source vintage points at (None when the
    chain breaks). One query for any number of observations, so callers batch a whole
    snapshot instead of one round-trip per member (Copilot on #776)."""
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
    """`verified` when every consumed observation dereferences to a landed raw fetch and every
    asserted input names its evidence; `degraded` when an asserted input has no evidence on
    the row; `rejected` when a pointer does not resolve.

    The observation → `raw.capture_source_vintages` → `raw.fetches` chain is what "the raw
    pointer dereferences" means. The headcount rule is the one input that enters the
    financial-fact payload from a side plane (`issuer_headcount_facts`, #70): until its
    evidence travels on the row (`vintage.headcount`, #530/#747 follow-up) a non-null
    headcount is asserted without evidence — `degraded`, honestly, not `verified` by
    omission.
    """
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
