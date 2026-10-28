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

from truealpha_contracts.fiscal_period import encode_annual
from truealpha_contracts.metrics import INPUT_KEY_ALIASES, METRICS, UnitFamily, input_key_for_metric

#: The input key of the captured close. It travels in the market-price payload, not in the financial one.
LAST_CLOSE_KEY = "last_close"

#: The cap on the confidence of a gross profit that the revenue proxy derived (#1255). Cause 1 of #1517
#: removes it. Until then this is its one copy: the seed, the projector and the core all read the cell.
REVENUE_PROXY_CONFIDENCE_CAP = Decimal("0.85")

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


def financial_input_rows(
    payload: Mapping[str, Any],
    confidence: Decimal,
    observed_at: datetime,
    *,
    knowable_at_of: KnowableAtOf | None = None,
) -> list[InputCell]:
    """The input cells that one financial-fact payload yields.

    The seed of the live tick, the historical projector (``datahub.strategy_history``) and the TOPT core
    snapshot (``production_topt.materialization``) call this function. All three get the same keys, the same
    period tags, the same units and the same confidence rule.

    The confidence of a gross profit that the revenue proxy derived is capped at
    ``REVENUE_PROXY_CONFIDENCE_CAP``. Every other cell carries ``confidence``.

    ``knowable_at_of(input_key, period_end)`` returns the filing time of one input. ``period_end`` is None
    for a scalar input. The default is ``live_knowable_at``. The projector passes ``vintage_knowable_at``
    instead: a None result drops the cell, because an input without a filing time has no knowable time and
    nothing may invent one.
    """
    stamp_of = live_knowable_at(payload, observed_at) if knowable_at_of is None else knowable_at_of
    currency = str(payload.get("currency", ""))
    capped = min(confidence, REVENUE_PROXY_CONFIDENCE_CAP) if _is_revenue_proxy(payload) else confidence
    cells: list[InputCell] = []
    for key in _STRATEGY_FINANCIAL_KEYS:
        value = payload.get(key)
        if value is not None and (stamp := stamp_of(key, None)) is not None:
            cells.append(
                InputCell(
                    key,
                    Decimal(str(value)),
                    unit_of(key, currency),
                    capped if key == "gross_profit" else confidence,
                    stamp,
                    None,
                )
            )
    for key, payload_key in _STRATEGY_PERIODIC_KEYS.items():
        # The period tag mirrors staging's own encoding so `factors.base.peg` parses one shape wherever the
        # series came from. Only the end date is known here, and an annual period's start is its end less a
        # year. The factor re-checks the duration floor rather than trusting the tag. One encoder, shared with
        # the factor that parses it (init.md Section 6): a tag built here and matched with a regex there is how
        # a format drifts into an empty series instead of an error.
        for period_end, value in sorted((payload.get(payload_key) or {}).items()):
            if (stamp := stamp_of(key, period_end)) is None:
                continue
            tag = encode_annual(date.fromisoformat(period_end))
            cells.append(InputCell(key, Decimal(str(value)), unit_of(key, currency), confidence, stamp, tag))
    return cells


def last_close_cell(payload: Mapping[str, Any], confidence: Decimal, observed_at: datetime) -> InputCell | None:
    """The cell of the captured close of one market-price payload, or None when the payload holds none."""
    close = payload.get("close")
    if close is None:
        return None
    return InputCell(
        LAST_CLOSE_KEY,
        Decimal(str(close)),
        unit_of(LAST_CLOSE_KEY, str(payload.get("currency", ""))),
        confidence,
        observed_at,
        None,
    )
