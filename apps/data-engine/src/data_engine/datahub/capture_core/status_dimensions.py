"""Producer-computed availability and input-evidence status dimensions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from psycopg import Connection
from truealpha_contracts.environment_contract import load_environment_contract
from truealpha_contracts.execution import AvailabilityStatus, InputEvidenceStatus
from truealpha_contracts.universes import universes_with_role

LOW_CONFIDENCE_FLOOR = Decimal("0.50")


def contract_not_applicable_issuer_ids(
    connection: Connection[Any],
    *,
    metric: str = "gppe",
    universe_id: str | None = None,
) -> frozenset[str]:
    """Resolve issuer identifiers declared not_applicable by the environment contract."""
    contract = load_environment_contract()
    entries = (
        tuple(entry for u in universes_with_role("strategy") for entry in contract.not_applicable_identities(metric, u))
        if universe_id is None
        else contract.not_applicable_for_universe_id(metric, universe_id)
    )
    if not entries:
        return frozenset()
    legacy_ids = list(dict.fromkeys(legacy_id for entry in entries for legacy_id in entry.legacy_ids))
    ciks = list(dict.fromkeys(entry.cik for entry in entries))
    leis = list(dict.fromkeys(entry.lei.upper() for entry in entries))
    resolved: set[str] = set(legacy_ids)
    rows = connection.execute(
        """
        select ei.entity_id::text, ei.legacy_id
        from mart.entity_identity ei
        where ei.cik = any(%s::text[])
           or upper(coalesce(ei.lei, '')) = any(%s::text[])
           or ei.legacy_id = any(%s::text[])
        """,
        (ciks, leis, legacy_ids),
    ).fetchall()
    for entity_id, legacy_id in rows:
        if entity_id:
            resolved.add(str(entity_id))
        if legacy_id:
            resolved.add(str(legacy_id))
    return frozenset(resolved)


def availability_status_for(
    *,
    availability: str,
    freshness: str,
    confidence: Decimal,
    reason_codes: Iterable[str] = (),
    not_applicable: bool = False,
) -> AvailabilityStatus:
    reasons = set(reason_codes)
    if availability == "unavailable":
        if not_applicable and "missing_gross_profit" in reasons:
            return AvailabilityStatus.NOT_APPLICABLE
        return AvailabilityStatus.UNAVAILABLE
    if freshness == "stale" or "stale_input" in reasons:
        return AvailabilityStatus.STALE
    if freshness == "unknown" or "unknown_freshness" in reasons:
        return AvailabilityStatus.ERROR
    if confidence < LOW_CONFIDENCE_FLOOR:
        return AvailabilityStatus.LOW_CONFIDENCE
    return AvailabilityStatus.AVAILABLE


def decision_availability_status(
    *,
    eligible: bool,
    exclusion_reason: str | None,
    value_present: bool,
    not_applicable: bool = False,
) -> AvailabilityStatus:
    if not eligible and not_applicable and exclusion_reason == "missing_gross_profit_fact":
        return AvailabilityStatus.NOT_APPLICABLE
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
) -> tuple[AvailabilityStatus, InputEvidenceStatus]:
    if not value_present:
        return (AvailabilityStatus.UNAVAILABLE, InputEvidenceStatus.DEGRADED)
    availability = (
        AvailabilityStatus.LOW_CONFIDENCE if confidence < LOW_CONFIDENCE_FLOOR else AvailabilityStatus.AVAILABLE
    )
    evidence = InputEvidenceStatus.VERIFIED if fully_evidenced else InputEvidenceStatus.DEGRADED
    return (availability, evidence)


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


def statuses_without_raw_pointer() -> InputEvidenceStatus:
    return InputEvidenceStatus.DEGRADED
