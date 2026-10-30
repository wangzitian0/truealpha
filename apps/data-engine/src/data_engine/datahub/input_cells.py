"""The input cells of one captured payload (#1518, step 4): one builder for every engine.

A cell is one input of one issuer: key, value, unit, confidence, the time it became knowable and
its fiscal period. The strategy seed, the history projector and the TOPT core snapshot build their
inputs from these cells. So the three engines share the confidence cap, the unit and the per-key
`knowable_at`. No engine keeps its own copy of these rules.

The confidence cap of a revenue-proxy gross profit lives here, and nowhere else, until Cause 1 of
#1517 removes it. The input age limit is `eligibility.maximum_input_age_days` of the strategy
definition. It applies to the `knowable_at` of each cell.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, NamedTuple

from factors.confidence import evaluate_cell_confidence
from truealpha_contracts.confidence import ContinuousConfidencePolicy, SourceConfidenceEvidence
from truealpha_contracts.fiscal_period import encode_annual
from truealpha_contracts.metrics import INPUT_KEY_ALIASES, METRICS, UnitFamily, input_key_for_metric

from data_engine.datahub.production_topt.source_registrations import source_reliability_prior

#: The input key of the captured close. It travels in the market-price payload, not in the financial one.
LAST_CLOSE_KEY = "last_close"

_CONFIDENCE_POLICY = ContinuousConfidencePolicy()

# Every registered metric except `price` surfaces as the scalar half of its financial-fact input: one
# cell with `fiscal_period` None, under the registry's own spelling (init.md rule 22, #770 finding 2).
# Adding metric N+1 to `truealpha_contracts.metrics.METRICS` needs no edit here. A periodic metric such
# as `net_income` is in this tuple too: that lands its latest value as a scalar, while
# `_STRATEGY_PERIODIC_KEYS` lands its whole annual series. `price` has its own payload and its own cell.
# A metric with no field on the payload yet (`cost_of_revenue`, `eps_diluted`) reads back None and is skipped.
_STRATEGY_FINANCIAL_KEYS = tuple(sorted(input_key_for_metric(name) for name in METRICS if name != "price"))

# Which metrics are period-shaped comes from the registry, never from a list here (init.md rule 22).
_STRATEGY_PERIODIC_KEYS = {name: f"{name}_by_period" for name, spec in METRICS.items() if spec.periodic}

#: The key of the per-period filing entries in the payload vintage: `<input key>_periods`.
_PERIOD_SERIES_SUFFIX = "_periods"

#: The unit of a count metric. A count has two meanings here, so the family alone cannot name it.
_COUNT_UNITS = {"employees_total": "employees", "shares_outstanding": "shares"}


class InputCell(NamedTuple):
    """One input of one issuer, as every engine reads it."""

    input_key: str
    value: Decimal
    unit: str
    confidence: Decimal
    #: When this input became knowable. Per key: the filing of this input, not of the newest input.
    knowable_at: datetime
    #: The fiscal period tag of a periodic input. None for a scalar input.
    fiscal_period: str | None


#: `(input_key, period_end)` -> when that input became knowable. `period_end` is None for a scalar input.
#: None means the payload names no filing for the input.
KnowableAtOf = Callable[[str, str | None], datetime | None]


def unit_of(input_key: str, currency: str) -> str:
    """The unit of an input: the currency, a count noun, or the currency per share."""
    metric = INPUT_KEY_ALIASES.get(input_key, input_key)
    spec = METRICS.get(metric)
    if spec is None:
        raise ValueError(
            f"{input_key!r} is not a registered metric (truealpha_contracts.metrics.METRICS); "
            "register it there before using it as a strategy input"
        )
    family = spec.unit_family
    if family is UnitFamily.CURRENCY:
        return currency
    if family is UnitFamily.COUNT:
        return _COUNT_UNITS[metric]
    if family is UnitFamily.PER_SHARE:
        return f"{currency}_per_share"
    return family.value


def end_of_day(day: date) -> datetime:
    """The last instant of a filed day: 23:59:59.999999Z."""
    return datetime.combine(day, time.max, tzinfo=UTC)


def _entry_knowable_at(entry: object) -> datetime | None:
    """The instant one vintage entry became known, or None when it names none.

    A headcount entry carries the ``knowable_at`` its extractor stamped. An XBRL entry carries
    the ``filed`` date of its filing, which is knowable at the end of that day.
    """
    if not isinstance(entry, Mapping):
        return None
    stamp = entry.get("knowable_at")
    if stamp:
        parsed = datetime.fromisoformat(str(stamp))
        if parsed.tzinfo is None:
            raise ValueError(f"vintage knowable_at {stamp!r} carries no time zone")
        return parsed
    filed = entry.get("filed")
    return end_of_day(date.fromisoformat(str(filed))) if filed else None


def vintage_knowable_at(payload: Mapping[str, Any]) -> KnowableAtOf:
    """The ``knowable_at_of`` callback that reads the filing time of each input from the payload vintage."""
    vintage = payload.get("vintage") or {}

    def knowable_at_of(input_key: str, period_end: str | None) -> datetime | None:
        if period_end is None:
            return _entry_knowable_at(vintage.get(input_key))
        return _entry_knowable_at((vintage.get(f"{input_key}{_PERIOD_SERIES_SUFFIX}") or {}).get(period_end))

    return knowable_at_of


def live_knowable_at(payload: Mapping[str, Any], observed_at: datetime) -> Callable[[str, str | None], datetime]:
    """The per-key filing time of a live observation, with the time of the observation as the last resort.

    The deployed adapter names a filing for every input that it fills. A payload without a filing for
    one input (an old payload, or a test fixture) gives that input the ``knowable_at`` of its
    observation, which is the newest filing of the payload and so never later than the cutoff.
    """
    named = vintage_knowable_at(payload)

    def knowable_at_of(input_key: str, period_end: str | None) -> datetime:
        stamp = named(input_key, period_end)
        return observed_at if stamp is None else stamp

    return knowable_at_of


def _is_revenue_proxy(payload: Mapping[str, Any]) -> bool:
    """True when the vintage names the gross profit as derived from revenue (#1255, #533)."""
    entry = (payload.get("vintage") or {}).get("gross_profit")
    return bool(isinstance(entry, Mapping) and entry.get("is_revenue_proxy", False))


def _evaluate_sec_field_confidence(key: str, payload: Mapping[str, Any]) -> Decimal:
    """Evaluate single-source SEC field confidence via `evaluate_cell_confidence` (#1517 B3)."""
    is_proxy = key == "gross_profit" and _is_revenue_proxy(payload)
    if key == "headcount":
        vintage = payload.get("vintage") or {}
        producer = str((vintage.get("headcount") or {}).get("source") or "10k-extraction")
        provider_id = f"origin:headcount:{producer}"
        lineage = "sec-10k"
    else:
        provider_id = "origin:sec-company-facts:v1"
        lineage = "sec-company-facts"
    prior = source_reliability_prior(provider_id) or source_reliability_prior("origin:sec-company-facts:v1")
    extra_codes = ("reason:single_origin", "confidence.revenue_proxy") if is_proxy else ("reason:single_origin",)
    return evaluate_cell_confidence(
        case_id=f"cell:{key}:{payload.get('listing_id') or payload.get('issuer_id') or 'sec'}",
        sources=(
            SourceConfidenceEvidence(
                provider_id=provider_id,
                origin_group_id=f"lineage:{lineage}",
                independence_weight=Decimal("1"),
                reliability_prior=prior,
                successful_outcome_mass=Decimal("0"),
                failed_outcome_mass=Decimal("0"),
                freshness=Decimal("1"),
                sample_conformance=Decimal("1"),
                transport_integrity=Decimal("1"),
                evidence_ids=(f"obs:{key}",),
                reason_codes=("reason:single_origin",),
            ),
        ),
        agreement=Decimal("1"),
        semantic_mapping_quality=Decimal("0.85") if is_proxy else Decimal("1"),
        lineage_completeness=Decimal("1"),
        required_component_completeness=Decimal("1"),
        extra_reason_codes=extra_codes,
        policy=_CONFIDENCE_POLICY,
    ).confidence


def _evaluate_close_confidence(payload: Mapping[str, Any]) -> Decimal:
    """Evaluate single-source close confidence via `evaluate_cell_confidence` (#1517 B3)."""
    served_by = payload.get("served_by_failover")
    origin_id = str(served_by) if served_by else "origin:yahoo:v1"
    lineage = origin_id.split(":")[1] if origin_id.startswith("origin:") and origin_id.count(":") >= 2 else "yahoo"
    prior = source_reliability_prior(origin_id) or source_reliability_prior("origin:yahoo:v1")
    return evaluate_cell_confidence(
        case_id=f"cell:close:{payload.get('listing_id') or 'listing'}",
        sources=(
            SourceConfidenceEvidence(
                provider_id=origin_id,
                origin_group_id=f"lineage:{lineage}",
                independence_weight=Decimal("1"),
                reliability_prior=prior,
                successful_outcome_mass=Decimal("0"),
                failed_outcome_mass=Decimal("0"),
                freshness=Decimal("1"),
                sample_conformance=Decimal("0.90") if served_by else Decimal("1"),
                transport_integrity=Decimal("1"),
                evidence_ids=("obs:close",),
                reason_codes=("reason:served_by_failover" if served_by else "reason:single_origin",),
            ),
        ),
        agreement=Decimal("1"),
        semantic_mapping_quality=Decimal("1"),
        lineage_completeness=Decimal("1"),
        required_component_completeness=Decimal("1"),
        extra_reason_codes=("reason:served_by_failover" if served_by else "reason:single_origin",),
        policy=_CONFIDENCE_POLICY,
    ).confidence


def financial_input_rows(
    payload: Mapping[str, Any],
    confidence: Decimal | None = None,
    observed_at: datetime | None = None,
    *,
    knowable_at_of: KnowableAtOf | None = None,
    confidence_by_key: Mapping[str, Decimal] | None = None,
) -> list[InputCell]:
    """The input cells that one financial-fact payload yields.

    The seed of the live tick, the historical projector (``datahub.strategy_history``) and the TOPT core
    snapshot (``production_topt.materialization``) call this function. All three get the same keys, the same
    period tags, the same units and the same confidence rule (#1517 B3).
    """
    if observed_at is None:
        observed_at = datetime.now(UTC)
    stamp_of = live_knowable_at(payload, observed_at) if knowable_at_of is None else knowable_at_of
    currency = str(payload.get("currency", ""))
    is_proxy = _is_revenue_proxy(payload)

    def _conf_for(key: str) -> Decimal:
        if confidence_by_key is not None and key in confidence_by_key:
            return confidence_by_key[key]
        if confidence is not None:
            if key == "gross_profit" and is_proxy:
                return (confidence * Decimal("0.85")).quantize(Decimal("0.0001"))
            return confidence
        return _evaluate_sec_field_confidence(key, payload)

    cells: list[InputCell] = []
    for key in _STRATEGY_FINANCIAL_KEYS:
        value = payload.get(key)
        if value is not None and (stamp := stamp_of(key, None)) is not None:
            cells.append(
                InputCell(
                    key,
                    Decimal(str(value)),
                    unit_of(key, currency),
                    _conf_for(key),
                    stamp,
                    None,
                )
            )
    for key, payload_key in _STRATEGY_PERIODIC_KEYS.items():
        key_conf = _conf_for(key)
        for period_end, value in sorted((payload.get(payload_key) or {}).items()):
            if (stamp := stamp_of(key, period_end)) is None:
                continue
            tag = encode_annual(date.fromisoformat(period_end))
            cells.append(InputCell(key, Decimal(str(value)), unit_of(key, currency), key_conf, stamp, tag))
    return cells


def last_close_cell(
    payload: Mapping[str, Any],
    confidence: Decimal | None = None,
    observed_at: datetime | None = None,
) -> InputCell | None:
    """The cell of the captured close of one market-price payload, or None when the payload holds none."""
    close = payload.get("close")
    if close is None:
        return None
    if observed_at is None:
        observed_at = datetime.now(UTC)
    resolved_conf = confidence if confidence is not None else _evaluate_close_confidence(payload)
    return InputCell(
        LAST_CLOSE_KEY,
        Decimal(str(close)),
        unit_of(LAST_CLOSE_KEY, str(payload.get("currency", ""))),
        resolved_conf,
        observed_at,
        None,
    )
