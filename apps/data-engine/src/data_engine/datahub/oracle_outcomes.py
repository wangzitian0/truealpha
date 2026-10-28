"""Record the outside comparisons of one capture run (#1523 S2).

A comparison of a served value with a witness value that this repository did not write is one
row of `mart.oracle_outcomes`. This module writes the rows of two comparators. Each reads stored
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

Both comparators judge `agree` when the ratio lies within the policy tolerance of 1
(`oracle_policy.v1.json`). The row stores the tolerance it was judged by. A run with no operator
list writes no operator row: a universe without a comparator is "no comparator", not a
disagreement. Nothing here is a gate. A `disagree` row is data. `record_oracle_outcomes_safely`
swallows every error and logs it, so a writer defect never stops a tick or a report.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, Protocol

import psycopg
from factors.production_topt import MetricAvailability, ToptCoreSnapshotInput
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
#: The reason of a `disagree` row whose served and witness values have opposite signs.
SIGN_DIFFERS = "sign_differs"

_FROZEN_FACTOR_INPUTS_SQL = """
    select member.factor_input
    from staging.topt_core_snapshots snapshot
    join staging.topt_core_snapshot_members member on member.snapshot_id = snapshot.snapshot_id
    where snapshot.run_id = %s
    order by member.snapshot_id, member.factor_input ->> 'issuer_id'
"""

#: The stored bytes behind each observation: the raw fetch of the source vintage that the observation cites.
_OBSERVATION_FETCH_SQL = """
    select observation.observation_id, vintage.raw_fetch_id
    from staging.capture_normalized_observations observation
    join raw.capture_source_vintages vintage on vintage.source_vintage_id = observation.source_vintage_id
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
    if metric.unit != INDEPENDENT_UNIT:
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
    fetch_of: dict[str, int] = dict(connection.execute(_OBSERVATION_FETCH_SQL, (wanted,)).fetchall())
    tolerance = load_oracle_policy().relative_tolerance(INDEPENDENT_COMPARATOR)
    day = cutoff.astimezone(UTC).date()
    outcomes: list[Outcome] = []
    for factor_input in factor_inputs:
        observation = observations[factor_input.issuer_id]
        fetch_id = None if observation is None else fetch_of.get(observation)
        facts = None if fetch_id is None else stored_document(connection, fetch_id)
        outcomes.extend(
            compare_independent_parse(factor_input, field, facts, cutoff=day, tolerance=tolerance)
            for field in INDEPENDENT_PARSERS
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
