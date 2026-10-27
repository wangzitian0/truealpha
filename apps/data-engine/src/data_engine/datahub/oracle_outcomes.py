"""Record the outside comparisons of one capture run (#1523 S2a).

A comparison of a served value with a witness value that this repository did not write is one
row of `mart.oracle_outcomes`. This module writes the rows of one comparator: the index operator's
market value, as price-cancelled listed units, per issuer.

* Witness: the operator quotes a market value and a last sale price for every listing of its list
  (`staging.etf_constituent_facts`). Their quotient is the unit count at the operator's own price.
  Dividing by the price cancels the price drift between the operator's last sale and our close.
* Served: the share count that the frozen snapshot multiplies by the close. A dual-listed issuer
  with one identical count on every listing holds that count once (the rule of
  `compute_topt_core`); counts that differ add up.
* Ratio: served divided by witness. `agree` when the ratio lies within the policy tolerance of 1
  (`oracle_policy.v1.json`); the row stores the tolerance it was judged by.

The function makes no vendor call. It reads stored rows only. A run with no operator list writes
nothing: a universe without a comparator is "no comparator", not a disagreement. Nothing here is a
gate. A `disagree` row is data. `record_oracle_outcomes_safely` swallows every error and logs it,
so a writer defect never stops a tick or a report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

import psycopg
from factors.production_topt import MetricAvailability, ToptCoreSnapshotInput
from truealpha_contracts.models import DataSource
from truealpha_contracts.oracle_policy import load_oracle_policy

from data_engine.datahub.production_topt.universe_plane import UNIVERSE_SOURCES

COMPARATOR = "operator_listed_units"
FIELD = "listed_units"
ORACLE_KIND = "operator"

AGREE = "agree"
DISAGREE = "disagree"
NOT_COMPARABLE = "not_comparable"

#: The reasons of a `not_comparable` row.
NO_SERVED_VALUE = "no_served_value"
NO_OPERATOR_ROW = "no_operator_row"
NO_MARKET_CAP = "no_market_cap"
NO_PRICE = "no_price"

_FROZEN_FACTOR_INPUTS_SQL = """
    select member.factor_input
    from staging.topt_core_snapshots snapshot
    join staging.topt_core_snapshot_members member on member.snapshot_id = snapshot.snapshot_id
    where snapshot.run_id = %s
    order by member.snapshot_id, member.factor_input ->> 'issuer_id'
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
        return _not_comparable(factor_input, list_as_of.isoformat(), served, NO_OPERATOR_ROW)
    period = figure.as_of.isoformat()
    if figure.market_cap is None:
        return _not_comparable(factor_input, period, served, NO_MARKET_CAP, figure=figure)
    if figure.last_sale_price is None or figure.last_sale_price <= 0:
        return _not_comparable(factor_input, period, served, NO_PRICE, figure=figure)
    witness = figure.market_cap / figure.last_sale_price
    if served is None:
        return _not_comparable(factor_input, period, None, NO_SERVED_VALUE, figure=figure, witness=witness)
    ratio = served / witness
    result = AGREE if abs(ratio - 1) <= tolerance else DISAGREE
    return Outcome(
        subject_id=factor_input.issuer_id,
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


def _not_comparable(
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


def record_oracle_outcomes(connection: psycopg.Connection[Any], run_id: str, *, compared_at: datetime) -> int:
    """Append the comparisons of run `run_id`; returns the number of rows written.

    `compared_at` is the clock of the run that compares (the tick's time, the report's time), never an
    insertion default. A second call for the same run writes nothing: the table's key drops a repeat.
    """
    status = connection.execute("select cutoff from mart.topt_capture_status where run_id = %s", (run_id,)).fetchone()
    if status is None:
        return 0
    figures, list_as_of = operator_figures(connection, status[0])
    if list_as_of is None:
        return 0  # no operator list is knowable at the cutoff: this run has no comparator
    factor_inputs = [
        ToptCoreSnapshotInput.model_validate(row[0]) for row in connection.execute(_FROZEN_FACTOR_INPUTS_SQL, (run_id,))
    ]
    tolerance = load_oracle_policy().relative_tolerance(COMPARATOR)
    tickers = run_tickers(connection, run_id)
    # The writer waits at most five seconds for a lock on the table it appends to.
    connection.execute("set local lock_timeout = '5s'")
    written = 0
    for factor_input in factor_inputs:
        outcome = compare_listed_units(
            factor_input,
            figures,
            ticker=tickers.get(factor_input.listing_id),
            list_as_of=list_as_of,
            tolerance=tolerance,
        )
        cursor = connection.execute(
            _INSERT_SQL,
            {
                "run_id": run_id,
                "subject_id": outcome.subject_id,
                "field": FIELD,
                "period": outcome.period,
                "served_value": outcome.served_value,
                "oracle_value": outcome.oracle_value,
                "oracle_kind": ORACLE_KIND,
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
