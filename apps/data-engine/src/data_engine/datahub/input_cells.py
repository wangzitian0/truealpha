"""Captured-payload input cell builder (#1518)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any, NamedTuple

from truealpha_contracts.fiscal_period import encode_annual
from truealpha_contracts.metrics import INPUT_KEY_ALIASES, METRICS, UnitFamily, input_key_for_metric

from data_engine.datahub._helpers import single_origin_confidence
from data_engine.datahub.capture_core.source_registrations import AsOfAxis, knowable_instant

LAST_CLOSE_KEY = "last_close"
_STRATEGY_FINANCIAL_KEYS = tuple(sorted(input_key_for_metric(name) for name in METRICS if name != "price"))
_STRATEGY_PERIODIC_KEYS = {name: f"{name}_by_period" for name, spec in METRICS.items() if spec.periodic}
_COUNT_UNITS = {
    "employees_total": "employees",
    "shares_outstanding": "shares",
    "shares_weighted_average_basic": "shares",
}


class InputCell(NamedTuple):
    input_key: str
    value: Decimal
    unit: str
    confidence: Decimal
    knowable_at: datetime
    fiscal_period: str | None


KnowableAtOf = Callable[[str, str | None], datetime | None]


def unit_of(input_key: str, currency: str) -> str:
    metric = INPUT_KEY_ALIASES.get(input_key, input_key)
    spec = METRICS.get(metric)
    if spec is None:
        raise ValueError(f"{input_key!r} is not a registered metric (truealpha_contracts.metrics.METRICS)")
    if spec.unit_family is UnitFamily.CURRENCY:
        return currency
    if spec.unit_family is UnitFamily.COUNT:
        return _COUNT_UNITS[metric]
    if spec.unit_family is UnitFamily.PER_SHARE:
        return f"{currency}_per_share"
    return spec.unit_family.value


def _entry_knowable_at(entry: object) -> datetime | None:
    if not isinstance(entry, Mapping):
        return None
    if stamp := entry.get("knowable_at"):
        parsed = datetime.fromisoformat(str(stamp))
        if parsed.tzinfo is None:
            raise ValueError(f"vintage knowable_at {stamp!r} carries no time zone")
        return parsed
    filed = entry.get("filed")
    return knowable_instant(AsOfAxis.ACCESSION, date.fromisoformat(str(filed))) if filed else None


def vintage_knowable_at(payload: Mapping[str, Any]) -> KnowableAtOf:
    vintage = payload.get("vintage") or {}

    def knowable_at_of(input_key: str, period_end: str | None) -> datetime | None:
        bucket = vintage if period_end is None else (vintage.get(f"{input_key}_periods") or {})
        return _entry_knowable_at(bucket.get(input_key if period_end is None else period_end))

    return knowable_at_of


def live_knowable_at(payload: Mapping[str, Any], observed_at: datetime) -> Callable[[str, str | None], datetime]:
    named = vintage_knowable_at(payload)
    return lambda input_key, period_end: stamp if (stamp := named(input_key, period_end)) is not None else observed_at


def _evaluate_sec_field_confidence(key: str, payload: Mapping[str, Any]) -> Decimal:
    producer = str(((payload.get("vintage") or {}).get("headcount") or {}).get("source") or "10k-extraction")
    provider_id = f"origin:headcount:{producer}" if key == "headcount" else "origin:sec-company-facts:v1"
    lineage = "sec-10k" if key == "headcount" else "sec-company-facts"
    subject = payload.get("listing_id") or payload.get("issuer_id") or "sec"
    return single_origin_confidence(
        case_id=f"cell:{key}:{subject}",
        provider_id=provider_id,
        lineage=lineage,
        fallback_prior_id="origin:sec-company-facts:v1",
        evidence_id=f"obs:{key}",
        extra_reason_codes=("reason:single_origin",),
    )


def _evaluate_close_confidence(payload: Mapping[str, Any]) -> Decimal:
    served_by = payload.get("served_by_failover")
    origin_id = str(served_by) if served_by else "origin:yahoo:v1"
    lineage = origin_id.split(":")[1] if origin_id.startswith("origin:") and origin_id.count(":") >= 2 else "yahoo"
    reason = "reason:served_by_failover" if served_by else "reason:single_origin"
    return single_origin_confidence(
        case_id=f"cell:close:{payload.get('listing_id') or 'listing'}",
        provider_id=origin_id,
        lineage=lineage,
        fallback_prior_id="origin:yahoo:v1",
        evidence_id="obs:close",
        sample_conformance=Decimal("0.90") if served_by else Decimal("1"),
        reason_codes=(reason,),
        extra_reason_codes=(reason,),
    )


def financial_input_rows(
    payload: Mapping[str, Any],
    confidence: Decimal | None = None,
    observed_at: datetime | None = None,
    *,
    knowable_at_of: KnowableAtOf | None = None,
    confidence_by_key: Mapping[str, Decimal] | None = None,
    skipped_out: list[str] | None = None,
) -> list[InputCell]:
    if observed_at is None and knowable_at_of is None:
        raise ValueError("financial_input_rows requires observed_at or knowable_at_of (no wall-clock default)")
    stamp_of = (
        live_knowable_at(payload, observed_at) if knowable_at_of is None and observed_at is not None else knowable_at_of
    )
    assert stamp_of is not None
    currency = str(payload.get("currency", ""))

    def _conf_for(key: str) -> Decimal:
        if confidence_by_key is not None and key in confidence_by_key:
            return confidence_by_key[key]
        return confidence if confidence is not None else _evaluate_sec_field_confidence(key, payload)

    cells: list[InputCell] = []
    for key in _STRATEGY_FINANCIAL_KEYS:
        value = payload.get(key)
        if value is not None:
            if (stamp := stamp_of(key, None)) is not None:
                cells.append(InputCell(key, Decimal(str(value)), unit_of(key, currency), _conf_for(key), stamp, None))
            elif skipped_out is not None:
                skipped_out.append(key)
    for key, payload_key in _STRATEGY_PERIODIC_KEYS.items():
        key_conf = _conf_for(key)
        for period_end, value in sorted((payload.get(payload_key) or {}).items()):
            if (stamp := stamp_of(key, period_end)) is not None:
                tag = encode_annual(date.fromisoformat(period_end))
                cells.append(InputCell(key, Decimal(str(value)), unit_of(key, currency), key_conf, stamp, tag))
            elif skipped_out is not None:
                skipped_out.append(f"{key}:{period_end}")
    return cells


def last_close_cell(payload: Mapping[str, Any], confidence: Decimal | None, observed_at: datetime) -> InputCell | None:
    close = payload.get("close")
    if close is None:
        return None
    resolved_conf = confidence if confidence is not None else _evaluate_close_confidence(payload)
    return InputCell(
        LAST_CLOSE_KEY,
        Decimal(str(close)),
        unit_of(LAST_CLOSE_KEY, str(payload.get("currency", ""))),
        resolved_conf,
        observed_at,
        None,
    )
