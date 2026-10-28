"""Record the outside comparisons of one capture run (#1523 S2).

A comparison of a served value with a witness value that this repository did not write is one
row of `mart.oracle_outcomes`. This module writes the rows of three comparators. Each reads stored
rows and stored bytes only and makes no vendor call.

Operator listed units, per issuer (`operator`):

* Witness: the operator quotes a market value and a last sale price for every listing of its list
  (`staging.etf_constituent_facts`). Their quotient is the unit count at the operator's own price.
  Dividing by the price cancels the price drift between the operator's last sale and our close.
* Served: the share count that the frozen snapshot multiplies by the close. A dual-listed issuer
  with one identical count on every listing holds that count once (the rule of
  `compute_topt_core`); counts that differ add up.

Independent parse of revenue and gross profit, per issuer and field (`independent_parse`):

* Witness: the stored SEC company-facts document behind the financial-fact observation of the
  served figure, parsed again by `quality.vendor_oracle`. That parser shares no mapping with the
  production parser: it reads more concept variants and takes the latest annual period across all
  of them. The row period is the period of the witness. `knowable_at` is the day it was filed.
* Served: the figure of the frozen snapshot. A served figure of an older period than the witness
  is a different number, so it reads `disagree`.
* The ratio is served divided by witness. When the two have opposite signs the ratio is the
  quotient of their magnitudes, because the table holds no negative ratio, and the row says
  `sign_differs`.

Sentinel known answers, per sentinel issuer and registered metric (`filing`):

* Witness: independent filing-derived known answers from `sentinel_known_answers.v1.json`.
* Served: the served core factor results (`mart.topt_core_results` for `gross_profit_per_employee`)
  or captured financial-fact observation payloads (`revenue`, `gross_profit`, `total_assets`,
  `shares_outstanding`, `employees_total`).
* When the served period end is newer than the answer's period end, the row is `not_comparable` with
  reason `superseded`.
* When comparable, ratio is served divided by oracle expectation (or band midpoint), judged against
  the policy tolerance or the answer's relative tolerance.

All three comparators judge `agree` when the ratio lies within the policy tolerance of 1
(`oracle_policy.v1.json`). The row stores the tolerance it was judged by. A run with no operator
list writes no operator row: a universe without a comparator is "no comparator", not a
disagreement. Nothing here is a gate. A `disagree` row is data. `record_oracle_outcomes_safely`
swallows every error and logs it, so a writer defect never stops a tick or a report.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, Protocol

import psycopg
from factors.production_topt import MetricAvailability, ToptCoreSnapshotInput
from truealpha_contracts.environment_contract import (
    SentinelKnownAnswer,
    load_sentinel_known_answers,
)
from truealpha_contracts.metrics import INPUT_KEY_ALIASES, METRICS
from truealpha_contracts.models import DataSource
from truealpha_contracts.oracle_policy import load_oracle_policy
from truealpha_runtime.storage import StorageError

from data_engine import raw_store
from data_engine.datahub.production_topt.universe_plane import UNIVERSE_SOURCES
from data_engine.quality import vendor_oracle

OPERATOR_COMPARATOR = "operator_listed_units"
OPERATOR_FIELD = "listed_units"
OPERATOR_KIND = "operator"

INDEPENDENT_COMPARATOR = "independent_parse_annual_figures"
INDEPENDENT_KIND = "independent_parse"
#: The served fields that an independent parse re-derives, each with its parser in `quality.vendor_oracle`.
INDEPENDENT_PARSERS: dict[str, Callable[[dict[str, Any], date], vendor_oracle.VendorFact | None]] = {
    "revenue": vendor_oracle.revenue,
    "gross_profit": vendor_oracle.gross_profit,
}
#: The unit of the figures that the independent parse reads.
INDEPENDENT_UNIT = "USD"

KNOWN_ANSWERS_COMPARATOR = "sentinel_known_answers"
FILING_KIND = "filing"
FILING_SOURCE = "sec-edgar-filing"

AGREE = "agree"
DISAGREE = "disagree"
NOT_COMPARABLE = "not_comparable"

#: The reasons of a `not_comparable` row.
NO_SERVED_VALUE = "no_served_value"
NO_OPERATOR_ROW = "no_operator_row"
NO_MARKET_CAP = "no_market_cap"
NO_PRICE = "no_price"
NO_DOCUMENT = "no_document"
NO_ORACLE_VALUE = "no_oracle_value"
ZERO_ORACLE_VALUE = "zero_oracle_value"
UNIT_NOT_USD = "unit_not_usd"
SUPERSEDED = "superseded"
PRIOR_PERIOD = "prior_period"
NO_OBSERVATION = "no_observation"
#: The reason of a `disagree` row whose served and witness values have opposite signs.
SIGN_DIFFERS = "sign_differs"

_FROZEN_FACTOR_INPUTS_SQL = """
    select member.factor_input
    from staging.topt_core_snapshots snapshot
    join staging.topt_core_snapshot_members member on member.snapshot_id = snapshot.snapshot_id
    where snapshot.run_id = %s
    order by member.snapshot_id, member.factor_input ->> 'issuer_id'
"""

#: The stored bytes and currency behind each observation: the raw fetch of the source vintage that the observation cites.
_OBSERVATION_FETCH_SQL = """
    select observation.observation_id, vintage.raw_fetch_id, payload.normalized_payload ->> 'currency'
    from staging.capture_normalized_observations observation
    join raw.capture_source_vintages vintage on vintage.source_vintage_id = observation.source_vintage_id
    left join staging.capture_observation_payloads payload on payload.observation_id = observation.observation_id
    where observation.observation_id = any(%s) and vintage.raw_fetch_id is not null
"""

_INSERT_SQL = """
    insert into mart.oracle_outcomes
        (run_id, subject_id, field, period, served_value, oracle_value, oracle_kind, source, observation_id,
         knowable_at, ratio, tolerance, result, reason, compared_at)
    values (%(run_id)s, %(subject_id)s, %(field)s, %(period)s, %(served_value)s, %(oracle_value)s, %(oracle_kind)s,
            %(source)s, %(observation_id)s, %(knowable_at)s, %(ratio)s, %(tolerance)s, %(result)s, %(reason)s,
            %(compared_at)s)
    on conflict on constraint oracle_outcomes_once do nothing
"""


class Logger(Protocol):
    def warning(self, message: str) -> None: ...


@dataclass(frozen=True)
class OperatorFigure:
    """One listing's row of the operator's list, as stored."""

    row_id: int
    ticker: str
    as_of: date
    source: str
    market_cap: Decimal | None
    last_sale_price: Decimal | None
    knowable_at: datetime


@dataclass(frozen=True)
class Outcome:
    """One comparison, as `mart.oracle_outcomes` stores it (the run and the clock are the caller's)."""

    subject_id: str
    field: str
    oracle_kind: str
    period: str
    served_value: Decimal | None
    oracle_value: Decimal | None
    source: str
    observation_id: str | None
    knowable_at: datetime | None
    ratio: Decimal | None
    tolerance: Decimal | None
    result: str
    reason: str | None


def run_tickers(connection: psycopg.Connection[Any], run_id: str) -> dict[str, str]:
    """The ticker of every listing of the run, as the run's own identity observations state it.

    A listing id of a snapshot is an opaque identifier. The ticker is the operator's key, and the
    release-derived identity observations of the run carry both.
    """
    rows = connection.execute(
        """
        select distinct p.normalized_payload ->> 'listing_id', upper(p.normalized_payload ->> 'ticker')
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_observation_payloads p on p.observation_id = oo.observation_id
        where ob.run_id = %s
          and p.normalized_payload ->> 'listing_id' is not null
          and p.normalized_payload ->> 'ticker' is not null
        """,
        (run_id,),
    ).fetchall()
    return {str(listing_id): str(ticker) for listing_id, ticker in rows}


def served_listed_units(factor_input: ToptCoreSnapshotInput) -> Decimal | None:
    """The share count that the frozen snapshot multiplies by the close, or None when it holds none.

    The core makes an issuer unavailable when one listing has no count, so one missing count is a
    missing value. One identical count on several listings is the issuer total and counts once.
    """
    counts: list[Decimal] = []
    for component in factor_input.market_value_components:
        metric = component.shares_outstanding
        if metric.availability is not MetricAvailability.AVAILABLE or metric.value is None:
            return None
        counts.append(metric.value)
    if not counts:
        return None
    return counts[0] if len(counts) > 1 and len(set(counts)) == 1 else sum(counts, start=Decimal(0))


def compare_listed_units(
    factor_input: ToptCoreSnapshotInput,
    figures: dict[str, OperatorFigure],
    *,
    ticker: str | None,
    list_as_of: date,
    tolerance: Decimal,
) -> Outcome:
    """Compare the served unit count of one issuer with the operator's figure of its execution listing."""
    served = served_listed_units(factor_input)
    figure = None if ticker is None else figures.get(ticker)
    if figure is None:
        return _operator_not_comparable(factor_input, list_as_of.isoformat(), served, NO_OPERATOR_ROW)
    period = figure.as_of.isoformat()
    if figure.market_cap is None:
        return _operator_not_comparable(factor_input, period, served, NO_MARKET_CAP, figure=figure)
    if figure.last_sale_price is None or figure.last_sale_price <= 0:
        return _operator_not_comparable(factor_input, period, served, NO_PRICE, figure=figure)
    witness = figure.market_cap / figure.last_sale_price
    if served is None:
        return _operator_not_comparable(factor_input, period, None, NO_SERVED_VALUE, figure=figure, witness=witness)
    ratio = served / witness
    result = AGREE if abs(ratio - 1) <= tolerance else DISAGREE
    return Outcome(
        subject_id=factor_input.issuer_id,
        field=OPERATOR_FIELD,
        oracle_kind=OPERATOR_KIND,
        period=period,
        served_value=served,
        oracle_value=witness,
        source=figure.source,
        observation_id=f"etf-constituent-fact:{figure.row_id}",
        knowable_at=figure.knowable_at,
        ratio=ratio,
        tolerance=tolerance,
        result=result,
        reason=None,
    )


def _operator_not_comparable(
    factor_input: ToptCoreSnapshotInput,
    period: str,
    served: Decimal | None,
    reason: str,
    *,
    figure: OperatorFigure | None = None,
    witness: Decimal | None = None,
) -> Outcome:
    return Outcome(
        subject_id=factor_input.issuer_id,
        field=OPERATOR_FIELD,
        oracle_kind=OPERATOR_KIND,
        period=period,
        served_value=served,
        oracle_value=witness,
        source=figure.source if figure is not None else DataSource.NASDAQ_INDEX.value,
        observation_id=None if figure is None else f"etf-constituent-fact:{figure.row_id}",
        knowable_at=None if figure is None else figure.knowable_at,
        ratio=None,
        tolerance=None,
        result=NOT_COMPARABLE,
        reason=reason,
    )


def operator_figures(
    connection: psycopg.Connection[Any], cutoff: datetime
) -> tuple[dict[str, OperatorFigure], date | None]:
    """The operator's newest list knowable at `cutoff`, by ticker, and its date; none when none exists.

    The lists are the index lists of `UNIVERSE_SOURCES` (a static member list holds no operator figure).
    """
    etfs = [source.etf for source in UNIVERSE_SOURCES.values() if source.index_api]
    figures: dict[str, OperatorFigure] = {}
    newest: date | None = None
    for etf in etfs:
        rows = connection.execute(
            """
            select id, ticker, as_of, source, market_cap, last_sale_price, knowable_at
            from staging.etf_constituent_facts
            where etf_symbol = %(etf)s
              and knowable_at <= %(cutoff)s
              and as_of = (
                  select max(as_of) from staging.etf_constituent_facts
                  where etf_symbol = %(etf)s and knowable_at <= %(cutoff)s
              )
            order by id
            """,
            {"etf": etf, "cutoff": cutoff},
        ).fetchall()
        for row_id, ticker, as_of, source, market_cap, last_sale_price, knowable_at in rows:
            figures.setdefault(
                str(ticker).upper(),
                OperatorFigure(int(row_id), str(ticker), as_of, str(source), market_cap, last_sale_price, knowable_at),
            )
            newest = as_of if newest is None else max(newest, as_of)
    return figures, newest


def operator_outcomes(
    connection: psycopg.Connection[Any],
    run_id: str,
    cutoff: datetime,
    factor_inputs: Sequence[ToptCoreSnapshotInput],
) -> list[Outcome]:
    """One operator row per issuer; none when no operator list is knowable at the cutoff."""
    figures, list_as_of = operator_figures(connection, cutoff)
    if list_as_of is None:
        return []  # no operator list is knowable at the cutoff: this run has no operator comparator
    tolerance = load_oracle_policy().relative_tolerance(OPERATOR_COMPARATOR)
    tickers = run_tickers(connection, run_id)
    return [
        compare_listed_units(
            factor_input,
            figures,
            ticker=tickers.get(factor_input.listing_id),
            list_as_of=list_as_of,
            tolerance=tolerance,
        )
        for factor_input in factor_inputs
    ]


def compare_independent_parse(
    factor_input: ToptCoreSnapshotInput,
    field: str,
    facts: dict[str, Any] | None,
    *,
    cutoff: date,
    tolerance: Decimal,
    currency: str | None = INDEPENDENT_UNIT,
) -> Outcome:
    """Compare the served `field` of one issuer with the independent parse of its stored SEC document.

    `facts` is the parsed document, or None when no document could be read. `cutoff` is the day
    that bounds the filings the parse may use.
    """
    metric = getattr(factor_input, field)
    served: Decimal | None = None if metric is None else metric.value
    observation_id: str | None = None if metric is None else metric.input_id

    def outcome(
        period: str,
        result: str,
        *,
        reason: str | None = None,
        witness: vendor_oracle.VendorFact | None = None,
        ratio: Decimal | None = None,
    ) -> Outcome:
        return Outcome(
            subject_id=factor_input.issuer_id,
            field=field,
            oracle_kind=INDEPENDENT_KIND,
            period=period,
            served_value=served,
            oracle_value=None if witness is None else witness.value,
            source=DataSource.SEC.value,
            observation_id=observation_id,
            knowable_at=None if witness is None else datetime.combine(witness.filed, time.min, tzinfo=UTC),
            ratio=ratio,
            tolerance=None if ratio is None else tolerance,
            result=result,
            reason=reason,
        )

    if facts is None:
        return outcome(cutoff.isoformat(), NOT_COMPARABLE, reason=NO_DOCUMENT)
    witness = INDEPENDENT_PARSERS[field](facts, cutoff)
    if witness is None:
        return outcome(cutoff.isoformat(), NOT_COMPARABLE, reason=NO_ORACLE_VALUE)
    period = witness.period_end.isoformat()
    if served is None:
        return outcome(period, NOT_COMPARABLE, reason=NO_SERVED_VALUE, witness=witness)
    if (currency or getattr(metric, "unit", INDEPENDENT_UNIT)) != INDEPENDENT_UNIT:
        return outcome(period, NOT_COMPARABLE, reason=UNIT_NOT_USD, witness=witness)
    if witness.value == 0:
        return outcome(period, NOT_COMPARABLE, reason=ZERO_ORACLE_VALUE, witness=witness)
    if served * witness.value < 0:
        ratio = abs(served) / abs(witness.value)
        return outcome(period, DISAGREE, reason=SIGN_DIFFERS, witness=witness, ratio=ratio)
    ratio = served / witness.value
    return outcome(period, AGREE if abs(ratio - 1) <= tolerance else DISAGREE, witness=witness, ratio=ratio)


def stored_document(connection: psycopg.Connection[Any], fetch_id: int) -> dict[str, Any] | None:
    """The stored company-facts document under `fetch_id`, or None when its bytes cannot be read.

    The object store verifies the checksum. A document that is lost, corrupt or not a JSON object
    is no witness: the row says `no_document`.
    """
    try:
        document = json.loads(raw_store.get_payload(connection, fetch_id))
    except (LookupError, StorageError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _financial_observation(factor_input: ToptCoreSnapshotInput) -> str | None:
    """The financial-fact observation that the served revenue and gross profit came from."""
    for field in INDEPENDENT_PARSERS:
        metric = getattr(factor_input, field)
        if metric is not None:
            return str(metric.input_id)
    return None


def independent_parse_outcomes(
    connection: psycopg.Connection[Any],
    cutoff: datetime,
    factor_inputs: Sequence[ToptCoreSnapshotInput],
) -> list[Outcome]:
    """One row per issuer and field: the whole head, re-parsed from its stored SEC bytes.

    One document is in memory at a time. Its rows are the only thing kept.
    """
    observations = {factor_input.issuer_id: _financial_observation(factor_input) for factor_input in factor_inputs}
    wanted = sorted({observation for observation in observations.values() if observation is not None})
    fetch_rows = connection.execute(_OBSERVATION_FETCH_SQL, (wanted,)).fetchall()
    fetch_of: dict[str, int] = {row[0]: row[1] for row in fetch_rows}
    currency_of: dict[str, str | None] = {row[0]: row[2] for row in fetch_rows}
    tolerance = load_oracle_policy().relative_tolerance(INDEPENDENT_COMPARATOR)
    day = cutoff.astimezone(UTC).date()
    outcomes: list[Outcome] = []
    for factor_input in factor_inputs:
        observation = observations[factor_input.issuer_id]
        fetch_id = None if observation is None else fetch_of.get(observation)
        currency = None if observation is None else currency_of.get(observation)
        facts = None if fetch_id is None else stored_document(connection, fetch_id)
        outcomes.extend(
            compare_independent_parse(factor_input, field, facts, cutoff=day, tolerance=tolerance, currency=currency)
            for field in INDEPENDENT_PARSERS
        )
    return outcomes


def canonical_metric(metric: str) -> str:
    """The canonical registered metric name, resolving aliases like gppe -> gross_profit_per_employee."""
    return INPUT_KEY_ALIASES.get(metric, metric)


def _compare_band_answer(
    answer: SentinelKnownAnswer,
    served_value: Decimal,
) -> tuple[Decimal, Decimal, Decimal, str, str | None]:
    lower = Decimal(str(answer.lower_bound))
    upper = Decimal(str(answer.upper_bound))
    midpoint = (lower + upper) / Decimal(2)
    tolerance = Decimal(str(answer.relative_tolerance)) if answer.relative_tolerance else (upper - midpoint) / midpoint
    result = AGREE if lower <= served_value <= upper else DISAGREE
    if midpoint == 0:
        ratio = Decimal(1) if served_value == 0 else Decimal(0)
        return midpoint, ratio, tolerance, result, None
    if served_value * midpoint < 0:
        ratio = abs(served_value) / abs(midpoint)
        return midpoint, ratio, tolerance, DISAGREE, SIGN_DIFFERS
    ratio = served_value / midpoint
    return midpoint, ratio, tolerance, result, None


def _compare_scalar_answer(
    answer: SentinelKnownAnswer,
    served_value: Decimal,
    policy_tolerance: Decimal,
) -> tuple[Decimal, Decimal, Decimal, str, str | None]:
    oracle_value = Decimal(str(answer.expected_value))
    tolerance = Decimal(str(answer.relative_tolerance)) if answer.relative_tolerance else policy_tolerance
    if oracle_value == 0:
        if served_value == 0:
            return oracle_value, Decimal(1), tolerance, AGREE, None
        return oracle_value, Decimal(0), tolerance, DISAGREE, None
    if served_value * oracle_value < 0:
        ratio = abs(served_value) / abs(oracle_value)
        return oracle_value, ratio, tolerance, DISAGREE, SIGN_DIFFERS
    ratio = served_value / oracle_value
    result = AGREE if abs(ratio - 1) <= tolerance else DISAGREE
    return oracle_value, ratio, tolerance, result, None


def compare_known_answer(
    answer: SentinelKnownAnswer,
    served_value: Decimal | None,
    *,
    subject_id: str | None = None,
    actual_period_end: str | None,
    observation_id: str | None,
    knowable_at: datetime | None,
    policy_tolerance: Decimal,
) -> Outcome:
    """Compare one sentinel known answer with the served value of the run (#1523 S2c)."""
    canonical_field = canonical_metric(answer.metric)
    source = answer.accession or FILING_SOURCE
    resolved_subject = subject_id or answer.issuer_id

    def outcome(
        result: str,
        *,
        reason: str | None = None,
        oracle_value: Decimal | None = None,
        ratio: Decimal | None = None,
        tolerance: Decimal | None = None,
        obs_id: str | None = observation_id,
        know_at: datetime | None = knowable_at,
    ) -> Outcome:
        return Outcome(
            subject_id=resolved_subject,
            field=canonical_field,
            oracle_kind=FILING_KIND,
            period=answer.period_end,
            served_value=served_value,
            oracle_value=oracle_value,
            source=source,
            observation_id=obs_id,
            knowable_at=know_at,
            ratio=ratio,
            tolerance=tolerance,
            result=result,
            reason=reason,
        )

    if actual_period_end is not None and str(actual_period_end) > answer.period_end:
        return outcome(NOT_COMPARABLE, reason=SUPERSEDED)

    if actual_period_end is not None and str(actual_period_end) < answer.period_end:
        return outcome(NOT_COMPARABLE, reason=PRIOR_PERIOD)

    if served_value is None:
        return outcome(NOT_COMPARABLE, reason=NO_SERVED_VALUE)

    if observation_id is None or knowable_at is None:
        return outcome(NOT_COMPARABLE, reason=NO_OBSERVATION, obs_id=None, know_at=None)

    if answer.kind == "band":
        oracle_val, ratio, tolerance, res, reason = _compare_band_answer(answer, served_value)
    else:
        oracle_val, ratio, tolerance, res, reason = _compare_scalar_answer(answer, served_value, policy_tolerance)

    return outcome(res, reason=reason, oracle_value=oracle_val, ratio=ratio, tolerance=tolerance)


def _query_gppe_by_issuer(
    connection: psycopg.Connection[Any],
    run_id: str,
    cutoff: datetime,
) -> dict[str, tuple[Decimal | None, str | None, str | None, datetime | None]]:
    gppe_rows = connection.execute(
        """
        select issuer_id, gppe, operating_period_end, input_observation_ids, cutoff, result_id
        from mart.topt_core_results
        where run_id = %s
        """,
        (run_id,),
    ).fetchall()
    gppe_by_issuer: dict[str, tuple[Decimal | None, str | None, str | None, datetime | None]] = {}
    for iss_id, gppe, op_period, obs_ids, cut, res_id in gppe_rows:
        obs_id = obs_ids[0] if obs_ids else res_id
        period_str = (
            op_period.isoformat()
            if isinstance(op_period, (date, datetime))
            else (str(op_period) if op_period else None)
        )
        entry = (gppe, period_str, obs_id, cut or cutoff)
        gppe_by_issuer[iss_id] = entry
        if str(iss_id).startswith("issuer:cik:"):
            gppe_by_issuer[str(iss_id).removeprefix("issuer:cik:")] = entry
    return gppe_by_issuer


def _query_fin_by_issuer(
    connection: psycopg.Connection[Any],
    run_id: str,
) -> dict[str, tuple[str, datetime, dict[str, Any]]]:
    fin_rows = connection.execute(
        """
        select
            coalesce(p.normalized_payload ->> 'issuer_id', ''),
            o.observation_id,
            o.knowable_at,
            p.normalized_payload
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        where ob.run_id = %s and o.semantic_type = 'financial-fact'
        order by o.observation_id
        """,
        (run_id,),
    ).fetchall()
    fin_by_issuer: dict[str, tuple[str, datetime, dict[str, Any]]] = {}
    for iss_id, obs_id, know_at, payload in fin_rows:
        # The newest knowable observation of an issuer wins. The observation id is a content hash,
        # so its order says nothing about time; it only breaks a tie of equal times.
        keys = [iss_id]
        if str(iss_id).startswith("issuer:cik:"):
            keys.append(str(iss_id).removeprefix("issuer:cik:"))
        for key in keys:
            held = fin_by_issuer.get(key)
            if held is None or (know_at, obs_id) >= (held[1], held[0]):
                fin_by_issuer[key] = (obs_id, know_at, payload)
    return fin_by_issuer


def _extract_metric_from_payload(
    payload: dict[str, Any],
    canonical_field: str,
) -> tuple[Decimal | None, str | None, datetime | None]:
    if canonical_field == "revenue":
        raw = payload.get("revenue")
        return (None if raw is None else Decimal(str(raw)), payload.get("revenue_period_end"), None)
    if canonical_field == "gross_profit":
        raw = payload.get("gross_profit")
        return (None if raw is None else Decimal(str(raw)), payload.get("operating_period_end"), None)
    if canonical_field == "shares_outstanding":
        raw = payload.get("shares_outstanding")
        return (None if raw is None else Decimal(str(raw)), payload.get("shares_period_end"), None)
    if canonical_field == "total_assets":
        raw = payload.get("total_assets")
        period = payload.get("operating_period_end") or payload.get("tangible_equity_period_end")
        return (None if raw is None else Decimal(str(raw)), period, None)
    if canonical_field == "employees_total":
        raw = payload.get("employees_total") or payload.get("headcount")
        val = None if raw is None else Decimal(str(raw))
        vintage = payload.get("vintage") or {}
        hc_vintage = vintage.get("headcount") or {}
        period = (
            hc_vintage.get("period_end") or payload.get("revenue_period_end") or payload.get("operating_period_end")
        )
        know_at = None
        if hc_vintage.get("knowable_at"):
            try:
                know_at = datetime.fromisoformat(hc_vintage["knowable_at"])
            except Exception:
                pass
        return (val, period, know_at)
    return (None, None, None)


def _extract_metric_from_factor_input(
    fi: ToptCoreSnapshotInput,
    canonical_field: str,
) -> tuple[Decimal | None, str | None, datetime | None]:
    metric_map = {
        "revenue": fi.revenue,
        "gross_profit": fi.gross_profit,
        "employees_total": fi.headcount,
        "total_assets": fi.total_assets,
    }
    metric = metric_map.get(canonical_field)
    if metric is not None:
        return (metric.value, str(metric.input_id), metric.knowable_at)
    return (None, None, None)


def run_issuer_tickers(connection: psycopg.Connection[Any], run_id: str) -> dict[str, str]:
    """The issuer_id of every ticker of the run, as the run's observations state it."""
    rows = connection.execute(
        """
        select distinct upper(p.normalized_payload ->> 'ticker'), p.normalized_payload ->> 'issuer_id'
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_observation_payloads p on p.observation_id = oo.observation_id
        where ob.run_id = %s
          and p.normalized_payload ->> 'issuer_id' is not null
          and p.normalized_payload ->> 'ticker' is not null
        """,
        (run_id,),
    ).fetchall()
    issuers: dict[str, str] = {str(ticker): str(issuer_id) for ticker, issuer_id in rows if ticker and issuer_id}
    listing_tickers = run_tickers(connection, run_id)
    if listing_tickers:
        fi_rows = connection.execute(
            """
            select member.factor_input
            from staging.topt_core_snapshots s
            join staging.topt_core_snapshot_members member on member.snapshot_id = s.snapshot_id
            where s.run_id = %s
            """,
            (run_id,),
        ).fetchall()
        for fi_row in fi_rows:
            fi = fi_row[0]
            if isinstance(fi, dict):
                lid = fi.get("listing_id")
                iid = fi.get("issuer_id")
                if lid and iid and lid in listing_tickers:
                    issuers.setdefault(listing_tickers[lid], str(iid))
    return issuers


def _resolve_answer_context(
    answer: SentinelKnownAnswer,
    canonical_field: str,
    *,
    issuer_id: str,
    gppe_by_issuer: dict[str, tuple[Decimal | None, str | None, str | None, datetime | None]],
    fin_by_issuer: dict[str, tuple[str, datetime, dict[str, Any]]],
    by_issuer_input: dict[str, ToptCoreSnapshotInput],
    cutoff: datetime,
) -> tuple[Decimal | None, str | None, str | None, datetime | None]:
    if canonical_field == "gross_profit_per_employee":
        gppe_entry = (
            gppe_by_issuer.get(issuer_id) or gppe_by_issuer.get(answer.issuer_id) or gppe_by_issuer.get(answer.cik)
        )
        if gppe_entry is not None:
            return gppe_entry
        fi = (
            by_issuer_input.get(issuer_id)
            or by_issuer_input.get(answer.issuer_id)
            or by_issuer_input.get(f"issuer:cik:{answer.cik}")
        )
        if fi is not None:
            obs = fi.observation_ids[0] if fi.observation_ids else None
            return (None, None, obs, cutoff)
        return (None, None, None, None)

    fin_entry = fin_by_issuer.get(issuer_id) or fin_by_issuer.get(answer.issuer_id) or fin_by_issuer.get(answer.cik)
    observation_id: str | None = None
    knowable_at: datetime | None = None
    actual_period_end: str | None = None
    served_value: Decimal | None = None

    if fin_entry is not None:
        observation_id, knowable_at, payload = fin_entry
        val, period, custom_know = _extract_metric_from_payload(payload, canonical_field)
        served_value = val
        actual_period_end = period
        if custom_know is not None:
            knowable_at = custom_know

    if served_value is None:
        fi = (
            by_issuer_input.get(issuer_id)
            or by_issuer_input.get(answer.issuer_id)
            or by_issuer_input.get(f"issuer:cik:{answer.cik}")
        )
        if fi is not None:
            val, obs_id, know = _extract_metric_from_factor_input(fi, canonical_field)
            if val is not None:
                served_value = val
                observation_id = observation_id or obs_id
                knowable_at = knowable_at or know

    return (served_value, actual_period_end, observation_id, knowable_at)


def known_answer_outcomes(
    connection: psycopg.Connection[Any],
    run_id: str,
    cutoff: datetime,
    factor_inputs: Sequence[ToptCoreSnapshotInput],
) -> list[Outcome]:
    """The known-answer comparisons of the sentinel issuers present in the run (#1523 S2c)."""
    catalog = load_sentinel_known_answers()
    try:
        policy_tolerance = load_oracle_policy().relative_tolerance(KNOWN_ANSWERS_COMPARATOR)
    except (KeyError, ValueError):
        policy_tolerance = Decimal("0.01")

    issuers_by_ticker = run_issuer_tickers(connection, run_id)
    by_issuer_input = {fi.issuer_id: fi for fi in factor_inputs}
    run_issuers = set(by_issuer_input) | set(issuers_by_ticker.values())
    run_ciks = {iss.removeprefix("issuer:cik:") for iss in run_issuers if iss.startswith("issuer:cik:")}

    gppe_by_issuer = _query_gppe_by_issuer(connection, run_id, cutoff)
    fin_by_issuer = _query_fin_by_issuer(connection, run_id)

    all_run_issuers = run_issuers | set(fin_by_issuer) | set(gppe_by_issuer)
    all_run_ciks = run_ciks | {k for k in all_run_issuers if not k.startswith("issuer:")}

    outcomes: list[Outcome] = []
    for answer in catalog.answers:
        canonical_field = canonical_metric(answer.metric)
        if canonical_field not in METRICS:
            continue
        issuer_id = (
            issuers_by_ticker.get(answer.ticker.upper())
            or (answer.issuer_id if answer.issuer_id in all_run_issuers else None)
            or (f"issuer:cik:{answer.cik}" if f"issuer:cik:{answer.cik}" in all_run_issuers else None)
        )
        if issuer_id is None and answer.cik not in all_run_ciks:
            continue

        target_subject_id = issuer_id or answer.issuer_id

        served_val, period, obs_id, know_at = _resolve_answer_context(
            answer,
            canonical_field,
            issuer_id=target_subject_id,
            gppe_by_issuer=gppe_by_issuer,
            fin_by_issuer=fin_by_issuer,
            by_issuer_input=by_issuer_input,
            cutoff=cutoff,
        )
        outcomes.append(
            compare_known_answer(
                answer,
                served_val,
                subject_id=target_subject_id,
                actual_period_end=period,
                observation_id=obs_id,
                knowable_at=know_at,
                policy_tolerance=policy_tolerance,
            )
        )
    return outcomes


def record_oracle_outcomes(connection: psycopg.Connection[Any], run_id: str, *, compared_at: datetime) -> int:
    """Append the comparisons of run `run_id`; returns the number of rows written.

    `compared_at` is the clock of the run that compares (the tick's time, the report's time), never an
    insertion default. A second call for the same run writes nothing: the table's key drops a repeat.
    """
    status = connection.execute("select cutoff from mart.topt_capture_status where run_id = %s", (run_id,)).fetchone()
    if status is None:
        return 0
    cutoff = status[0]
    factor_inputs = [
        ToptCoreSnapshotInput.model_validate(row[0]) for row in connection.execute(_FROZEN_FACTOR_INPUTS_SQL, (run_id,))
    ]
    outcomes = [
        *operator_outcomes(connection, run_id, cutoff, factor_inputs),
        *independent_parse_outcomes(connection, cutoff, factor_inputs),
        *known_answer_outcomes(connection, run_id, cutoff, factor_inputs),
    ]
    # The writer waits at most five seconds for a lock on the table it appends to.
    connection.execute("set local lock_timeout = '5s'")
    written = 0
    for outcome in outcomes:
        cursor = connection.execute(
            _INSERT_SQL,
            {
                "run_id": run_id,
                "subject_id": outcome.subject_id,
                "field": outcome.field,
                "period": outcome.period,
                "served_value": outcome.served_value,
                "oracle_value": outcome.oracle_value,
                "oracle_kind": outcome.oracle_kind,
                "source": outcome.source,
                "observation_id": outcome.observation_id,
                "knowable_at": outcome.knowable_at,
                "ratio": outcome.ratio,
                "tolerance": outcome.tolerance,
                "result": outcome.result,
                "reason": outcome.reason,
                "compared_at": compared_at,
            },
        )
        written += cursor.rowcount
    return written


def record_oracle_outcomes_safely(
    connection: psycopg.Connection[Any], run_id: str, *, compared_at: datetime, log: Logger
) -> int | None:
    """`record_oracle_outcomes` in a transaction of its own, after the caller's commit; never raises.

    A comparison is evidence, not a gate: a writer defect is logged and the tick or the report goes on.
    Returns the rows written, or None when the write failed.
    """
    try:
        with connection.transaction():
            return record_oracle_outcomes(connection, run_id, compared_at=compared_at)
    except Exception as error:
        log.warning(f"oracle outcomes of {run_id} were not written: {type(error).__name__}: {error}")
        return None


_WILSON_Z_95 = 1.959963984540054


def wilson_95_upper_bound(disagrees: int, compared: int) -> float | None:
    """Return the 95% Wilson score upper bound for `disagrees` over `compared`."""
    if compared <= 0:
        return None
    z2 = _WILSON_Z_95 * _WILSON_Z_95
    p = disagrees / compared
    centre = p + z2 / (2.0 * compared)
    half_width = _WILSON_Z_95 * math.sqrt((p * (1.0 - p) + z2 / (4.0 * compared)) / compared)
    denom = 1.0 + z2 / compared
    return min(1.0, round((centre + half_width) / denom, 6))


def _iso_utc(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    text = str(value).strip()
    return text or None


def _row_attr(row: Mapping[str, Any] | Outcome, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def _summarize_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    agree = sum(1 for item in rows if item["result"] == "agree")
    disagree = sum(1 for item in rows if item["result"] == "disagree")
    not_comparable = sum(1 for item in rows if item["result"] == "not_comparable")
    compared = agree + disagree
    timestamps = [item["compared_at_iso"] for item in rows if item["compared_at_iso"] is not None]
    newest_compared_at = max(timestamps) if timestamps else None
    return {
        "compared": compared,
        "agree": agree,
        "disagree": disagree,
        "not_comparable": not_comparable,
        "newest_compared_at": newest_compared_at,
        "wilson_95_upper_bound": wilson_95_upper_bound(disagree, compared),
    }


def rollup_oracle_outcomes(rows: Sequence[Mapping[str, Any] | Outcome]) -> dict[str, Any]:
    """Deduplicate oracle outcomes per `(observation_id, subject_id, field)` and compute rollup counts."""
    deduped: dict[tuple[str | None, str, str], dict[str, Any]] = {}
    deduped_by_comparator: dict[tuple[str, str | None, str, str], dict[str, Any]] = {}

    for row in rows:
        obs_raw = _row_attr(row, "observation_id")
        observation_id = str(obs_raw) if obs_raw is not None else None
        subject_id = str(_row_attr(row, "subject_id", ""))
        field = str(_row_attr(row, "field", ""))
        result = str(_row_attr(row, "result", ""))
        reason_raw = _row_attr(row, "reason")
        reason = str(reason_raw) if reason_raw is not None else None
        oracle_kind_raw = _row_attr(row, "oracle_kind")
        oracle_kind = str(oracle_kind_raw) if oracle_kind_raw is not None else None
        compared_at_iso = _iso_utc(_row_attr(row, "compared_at"))

        normalized = {
            "observation_id": observation_id,
            "subject_id": subject_id,
            "field": field,
            "result": result,
            "reason": reason,
            "oracle_kind": oracle_kind,
            "compared_at_iso": compared_at_iso,
        }
        if oracle_kind:
            comp_key = (oracle_kind, observation_id, subject_id, field)
            comp_existing = deduped_by_comparator.get(comp_key)
            if comp_existing is None or (compared_at_iso or "") >= (comp_existing["compared_at_iso"] or ""):
                deduped_by_comparator[comp_key] = normalized

        key = (observation_id, subject_id, field)
        existing = deduped.get(key)
        if existing is None or (compared_at_iso or "") >= (existing["compared_at_iso"] or ""):
            deduped[key] = normalized

    deduped_rows = list(deduped.values())
    totals = _summarize_counts(deduped_rows)

    by_field_rows: dict[str, list[dict[str, Any]]] = {}
    for item in deduped_rows:
        by_field_rows.setdefault(item["field"], []).append(item)
    by_field = {field: _summarize_counts(field_rows) for field, field_rows in sorted(by_field_rows.items())}

    by_comparator_rows: dict[str, list[dict[str, Any]]] = {}
    for item in deduped_by_comparator.values():
        by_comparator_rows.setdefault(str(item["oracle_kind"]), []).append(item)

    comparators: dict[str, dict[str, Any]] = {}
    for kind, kind_rows in sorted(by_comparator_rows.items()):
        if kind == "independent_parse" and all(r["reason"] == "no_document" for r in kind_rows):
            continue
        comparators[kind] = _summarize_counts(kind_rows)

    return {
        **totals,
        "by_field": by_field,
        "comparators": comparators,
    }


def load_oracle_outcomes_rollup(
    connection: psycopg.Connection[Any],
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Query `mart.oracle_outcomes` and return its deduplicated rollup."""
    if not hasattr(connection, "execute"):
        return rollup_oracle_outcomes(())
    try:
        if run_id is not None:
            cursor = connection.execute(
                """
                select observation_id, subject_id, field, result, reason, oracle_kind, compared_at
                from mart.oracle_outcomes
                where run_id = %s
                order by compared_at asc, recorded_at asc, outcome_id asc
                """,
                (run_id,),
            )
        else:
            cursor = connection.execute(
                """
                select observation_id, subject_id, field, result, reason, oracle_kind, compared_at
                from mart.oracle_outcomes
                order by compared_at asc, recorded_at asc, outcome_id asc
                """
            )
        raw_rows = cursor.fetchall()
    except Exception:
        return rollup_oracle_outcomes(())

    mapped = [
        {
            "observation_id": row[0],
            "subject_id": row[1],
            "field": row[2],
            "result": row[3],
            "reason": row[4],
            "oracle_kind": row[5],
            "compared_at": row[6],
        }
        for row in raw_rows
    ]
    return rollup_oracle_outcomes(mapped)


def _outcome_to_dict(row: Mapping[str, Any] | Outcome) -> dict[str, Any]:
    if isinstance(row, Outcome):
        return {
            "subject_id": row.subject_id,
            "field": row.field,
            "oracle_kind": row.oracle_kind,
            "period": row.period,
            "served_value": row.served_value,
            "oracle_value": row.oracle_value,
            "source": row.source,
            "observation_id": row.observation_id,
            "knowable_at": row.knowable_at,
            "ratio": row.ratio,
            "tolerance": row.tolerance,
            "result": row.result,
            "reason": row.reason,
            "compared_at": None,
        }
    return dict(row)


def _resolve_row_origin_id(row: Mapping[str, Any]) -> str | None:
    oracle_kind = str(row.get("oracle_kind") or "")
    field = canonical_metric(str(row.get("field") or ""))
    if oracle_kind == OPERATOR_KIND or field == "gross_profit_per_employee":
        return None
    for key in ("origin_id", "lineage", "origin"):
        explicit = row.get(key)
        if isinstance(explicit, str) and explicit.startswith("origin:"):
            return explicit
    source = str(row.get("source") or "")
    if source.startswith("origin:"):
        return source
    lowered = source.lower()
    if "moomoo" in lowered:
        return "origin:moomoo-financials:v1"
    if "twelve" in lowered:
        return "origin:twelve-data:v1"
    if "yahoo" in lowered:
        return "origin:yahoo:v1"
    if "nport" in lowered:
        return "origin:sec-nport:v1"
    if "nasdaq" in lowered:
        return "origin:nasdaq-index:v1"
    if oracle_kind in (FILING_KIND, INDEPENDENT_KIND):
        return "origin:sec-company-facts:v1"
    return None


def _is_at_or_before_cutoff(row: Mapping[str, Any], cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    cutoff_iso = _iso_utc(cutoff) or ""
    compared_iso = _iso_utc(row.get("compared_at"))
    if compared_iso is not None:
        return compared_iso <= cutoff_iso
    knowable_iso = _iso_utc(row.get("knowable_at"))
    if knowable_iso is not None:
        return knowable_iso <= cutoff_iso
    return True


def _row_compared_iso(item: Mapping[str, Any]) -> str:
    return _iso_utc(item.get("compared_at")) or _iso_utc(item.get("knowable_at")) or ""


def _dedupe_origin_mass_rows(
    rows: Sequence[Mapping[str, Any] | Outcome],
    cutoff: datetime | None,
) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for raw in rows:
        item = _outcome_to_dict(raw)
        if not _is_at_or_before_cutoff(item, cutoff):
            continue
        oracle_kind = str(item.get("oracle_kind") or "")
        observation_id = str(item.get("observation_id") or "")
        subject_id = str(item.get("subject_id") or "")
        field = canonical_metric(str(item.get("field") or ""))
        if not (oracle_kind and subject_id and field):
            continue
        compared_iso = _row_compared_iso(item)
        item["_compared_iso"] = compared_iso
        key = (oracle_kind, observation_id, subject_id, field)
        existing = deduped.get(key)
        if existing is None or compared_iso >= existing["_compared_iso"]:
            deduped[key] = item
    return list(deduped.values())


_OUTCOME_MASS_DELTAS: dict[tuple[str, str], tuple[Decimal, Decimal]] = {
    (FILING_KIND, AGREE): (Decimal("1"), Decimal("0")),
    (FILING_KIND, DISAGREE): (Decimal("0"), Decimal("1")),
    (INDEPENDENT_KIND, DISAGREE): (Decimal("0"), Decimal("1")),
}


def _accumulate_origin_mass(
    totals: dict[str, tuple[Decimal, Decimal]],
    origin_id: str,
    field: str,
    success_delta: Decimal,
    fail_delta: Decimal,
) -> None:
    if success_delta == 0 and fail_delta == 0:
        totals.setdefault(origin_id, (Decimal("0"), Decimal("0")))
        return
    keys = (
        (origin_id, f"{origin_id}:{field}", f"{origin_id}:headcount")
        if field == "employees_total"
        else (origin_id, f"{origin_id}:{field}")
    )
    for key in keys:
        succ, fail = totals.get(key, (Decimal("0"), Decimal("0")))
        totals[key] = (succ + success_delta, fail + fail_delta)


def oracle_outcome_mass_by_origin(
    rows: Sequence[Mapping[str, Any] | Outcome],
    *,
    cutoff: datetime | None = None,
) -> dict[str, tuple[Decimal, Decimal]]:
    """Compute (successful_outcome_mass, failed_outcome_mass) per origin from mart.oracle_outcomes (#1517, #1523).

    Deduplicates per (oracle_kind, observation_id, subject_id, field) at or before `cutoff`:
    - `filing` `agree` -> +1 success; `filing` `disagree` -> +1 fail.
    - `independent_parse` `disagree` -> +1 fail; `independent_parse` `agree` -> +0 success (init.md p.342).
    - `operator` / `gross_profit_per_employee` -> 0 to input origin mass.
    """
    totals: dict[str, tuple[Decimal, Decimal]] = {}
    for item in _dedupe_origin_mass_rows(rows, cutoff):
        origin_id = _resolve_row_origin_id(item)
        if origin_id is None:
            continue
        oracle_kind = str(item.get("oracle_kind") or "")
        result = str(item.get("result") or "")
        field = canonical_metric(str(item.get("field") or ""))
        success_delta, fail_delta = _OUTCOME_MASS_DELTAS.get(
            (oracle_kind, result),
            (Decimal("0"), Decimal("0")),
        )
        _accumulate_origin_mass(totals, origin_id, field, success_delta, fail_delta)
    return totals


def _relative_delta_from_row(row: Mapping[str, Any], result: str) -> Decimal | None:
    served_raw = row.get("served_value")
    oracle_raw = row.get("oracle_value")
    if served_raw is not None and oracle_raw is not None:
        served_val = Decimal(str(served_raw))
        oracle_val = Decimal(str(oracle_raw))
        if served_val * oracle_val < 0:
            return Decimal("1")
        denom = max(abs(served_val), abs(oracle_val))
        return Decimal("0") if denom == 0 else abs(served_val - oracle_val) / denom
    ratio_raw = row.get("ratio")
    if ratio_raw is None:
        return None
    ratio_val = Decimal(str(ratio_raw))
    if result == AGREE and Decimal("0") <= ratio_val < Decimal("0.5"):
        return ratio_val
    return abs(ratio_val - Decimal("1"))


def _score_from_delta(delta: Decimal, result: str) -> Decimal:
    if delta <= Decimal("0.001"):
        return Decimal("1") if result == AGREE else Decimal("0.90")
    if delta <= Decimal("0.01"):
        return Decimal("0.90")
    if delta <= Decimal("0.05"):
        return Decimal("0.65")
    return Decimal("0.25")


def _agreement_from_outcome_row(row: Mapping[str, Any]) -> Decimal:
    result = str(row.get("result") or "")
    if str(row.get("reason") or "") == SIGN_DIFFERS:
        return Decimal("0.25")
    delta = _relative_delta_from_row(row, result)
    if delta is None:
        return Decimal("1") if result == AGREE else Decimal("0.25")
    tolerance_raw = row.get("tolerance")
    if result == AGREE and tolerance_raw is not None:
        tol_val = Decimal(str(tolerance_raw))
        if tol_val > Decimal("0.01") and delta <= tol_val:
            return Decimal("1")
    return _score_from_delta(delta, result)


def _expand_subject_targets(subject_ids: Sequence[str] | str) -> set[str]:
    targets = {subject_ids} if isinstance(subject_ids, str) else {s for s in subject_ids if s}
    for s in list(targets):
        if s.startswith("issuer:cik:"):
            targets.add(s.removeprefix("issuer:cik:"))
        elif s.isdigit():
            targets.add(f"issuer:cik:{s.zfill(10)}")
    return targets


def _candidate_field_names(canonical_field: str, field: str) -> set[str]:
    candidates = {canonical_field, field}
    if canonical_field == "employees_total":
        candidates.add("headcount")
    return candidates


def _matches_cell_target(item: Mapping[str, Any], targets: set[str], candidate_fields: set[str]) -> bool:
    subj = str(item.get("subject_id") or "")
    if subj not in targets:
        return False
    row_field = str(item.get("field") or "")
    return row_field in candidate_fields or canonical_metric(row_field) in candidate_fields


def oracle_cell_agreement(
    rows: Sequence[Mapping[str, Any] | Outcome],
    *,
    subject_ids: Sequence[str] | str,
    field: str,
    cutoff: datetime | None = None,
) -> tuple[Decimal, tuple[str, ...]]:
    """Compute cell agreement and reason codes from comparable mart.oracle_outcomes rows (#1523 S4)."""
    targets = _expand_subject_targets(subject_ids)
    canonical_field = canonical_metric(field)
    candidate_fields = _candidate_field_names(canonical_field, field)

    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in rows:
        item = _outcome_to_dict(raw)
        if not _is_at_or_before_cutoff(item, cutoff) or not _matches_cell_target(item, targets, candidate_fields):
            continue
        subj = str(item.get("subject_id") or "")
        oracle_kind = str(item.get("oracle_kind") or "oracle")
        compared_iso = _row_compared_iso(item)
        item["_compared_iso"] = compared_iso
        key = (oracle_kind, subj, canonical_field)
        existing = deduped.get(key)
        if existing is None or compared_iso >= existing["_compared_iso"]:
            deduped[key] = item

    comparable = [item for item in deduped.values() if str(item.get("result") or "") in (AGREE, DISAGREE)]
    if not comparable:
        return Decimal("1"), ("oracle:none",)

    scores = [_agreement_from_outcome_row(item) for item in comparable]
    reasons = {"oracle:agree" if str(item.get("result") or "") == AGREE else "oracle:disagree" for item in comparable}
    return min(scores), tuple(sorted(reasons))


def load_oracle_confidence_inputs(
    connection: psycopg.Connection[Any],
    *,
    run_id: str | None = None,
    cutoff: datetime | None = None,
) -> list[dict[str, Any]]:
    """Load mart.oracle_outcomes rows at or before `cutoff` for confidence evaluation (#1517, #1523)."""
    if not hasattr(connection, "execute"):
        return []
    clauses: list[str] = []
    params: list[Any] = []
    if cutoff is not None:
        clauses.append("compared_at <= %s")
        params.append(cutoff)
    elif run_id is not None:
        clauses.append("run_id = %s")
        params.append(run_id)
    where_sql = f"where {' and '.join(clauses)}" if clauses else ""
    try:
        cursor = connection.execute(
            f"""
            select
                observation_id,
                subject_id,
                field,
                period,
                served_value,
                oracle_value,
                oracle_kind,
                source,
                knowable_at,
                ratio,
                tolerance,
                result,
                reason,
                compared_at
            from mart.oracle_outcomes
            {where_sql}
            order by compared_at asc, recorded_at asc, outcome_id asc
            """,
            tuple(params),
        )
        raw_rows = cursor.fetchall()
    except Exception:
        return []
    return [
        {
            "observation_id": row[0],
            "subject_id": row[1],
            "field": row[2],
            "period": row[3],
            "served_value": row[4],
            "oracle_value": row[5],
            "oracle_kind": row[6],
            "source": row[7],
            "knowable_at": row[8],
            "ratio": row[9],
            "tolerance": row[10],
            "result": row[11],
            "reason": row[12],
            "compared_at": row[13],
        }
        for row in raw_rows
    ]
