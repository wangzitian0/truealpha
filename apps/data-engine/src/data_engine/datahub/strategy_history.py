"""The point-in-time projector for historical strategy inputs (#1139, M3 track H1, A6).

``staging.strategy_backtest_inputs`` holds one forward snapshot per live tick. No code wrote a
row for a past cutoff, so a backtest over 36 monthly cutoffs had no input history. This module
builds those rows from bytes already stored: the SEC company-facts documents, the headcount
fact table and the daily unadjusted bars. It makes zero vendor calls.

Rules this module enforces:

* Cutoffs are the first calendar day of each of the last N months at 00:00:00Z. The tick time
  of the run derives them. The decision happens at the cutoff. The trade fills at the open of
  the first trading day of that month (A6).
* Strict point in time. A fact is admitted only when its filed date is before the calendar date
  of the cutoff. The row's ``knowable_at`` is the end of the filed day. A fact filed on the
  cutoff date therefore has ``knowable_at > cutoff_at``, and the table CHECK
  ``knowable_at <= cutoff_at`` rejects it.
* Each input carries its own filed date and its own fiscal period, exactly where the live
  writer does. There is no blended value.
* ``last_close`` is the unadjusted close (``adjust = 'none'``, #1131) of the last session
  before the cutoff date. Shares are as filed (A6 decision 4).
* The run is idempotent. The unique index of the table includes ``recorded_at``, so a second
  insert of the same fact would duplicate the row. The projector skips a row that exists for
  the same issuer, cutoff, input key and fiscal period with the same value and ``knowable_at``.

The financial inputs come from ``SecFinancialFactAdapter`` over the stored bytes, evaluated for
the day before the cutoff date. That is the same code the live tick runs, so the period gates
(#1114), the revenue proxy gate (#533) and the share staleness bound (#529) apply unchanged. The
rows come from ``strategy_bridge.financial_input_rows``, the function the live writer uses.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

import psycopg
from factors.production_topt import OperatingBranch
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.concept_mapping import ConceptMappingRuleset
from truealpha_contracts.datahub import CaptureWorkItem
from truealpha_contracts.metrics import is_registered_input_key

from data_engine import raw_store
from data_engine.datahub.production_topt.concept_mapping import resolve_ruleset
from data_engine.datahub.production_topt.executor import FetchSuccess
from data_engine.datahub.production_topt.headcount import PostgresHeadcountExtractor
from data_engine.datahub.production_topt.parser_identity import PARSER_VERSION
from data_engine.datahub.production_topt.sec_financial_adapter import (
    HeadcountExtractor,
    SecFinancialFactAdapter,
    SecTarget,
    build_bundle,
    company_facts_record_id,
    predecessor_ciks,
)
from data_engine.datahub.resolve_coordinates import alias_of, is_uuid, parse_alias
from data_engine.datahub.strategy_bridge import financial_input_rows

DEFAULT_MONTHS = 36

#: The ``adjust`` value of the unadjusted daily bars (#1131, ``market_prices.UNADJUSTED``).
#: Market value is the unadjusted close times the as-filed share count (A6 decision 4).
UNADJUSTED_BARS = "none"

_COMPANY_FACTS_PREFIX = "companyfacts:CIK"
#: The payload carries a period series as ``<metric>_by_period``. Its filings sit in the
#: vintage under ``<metric>_periods``.
_PERIOD_SERIES_SUFFIX = "_periods"

#: The input row this module writes beside the financial inputs.
_LAST_CLOSE_KEY = "last_close"

FactsLoader = Callable[[int], tuple[bytes, dict[str, Any]] | None]


class LookAheadError(ValueError):
    """An input is knowable on or after the calendar date of the cutoff."""


class LastClose(NamedTuple):
    """The close of one session and the instant it became known."""

    close: Decimal
    confidence: Decimal
    knowable_at: datetime


PriceReader = Callable[[str, datetime], LastClose | None]


@dataclass(frozen=True)
class HistoryIssuer:
    """What the projector needs to project one issuer, read from stored lineage."""

    issuer_id: str
    instrument_id: str
    listing_id: str
    ticker: str
    cik: int
    operating_branch: OperatingBranch
    revenue_proxy_allowed: bool = False
    predecessor_cik: int | None = None
    predecessor_signed: bool = False


@dataclass(frozen=True)
class HistoryRow:
    """One row of ``staging.strategy_backtest_inputs``, before insert."""

    issuer_id: str
    cutoff_at: datetime
    input_key: str
    value: Decimal
    confidence: Decimal
    knowable_at: datetime
    fiscal_period: str | None


@dataclass(frozen=True)
class Projection:
    """The rows of one issuer over many cutoffs, with the cases that produced no row."""

    rows: tuple[HistoryRow, ...]
    #: Cutoffs where the adapter returned no financial outcome (no stored document, or no fact).
    no_outcome: int = 0
    #: Inputs dropped because the payload names no filing for them.
    undated: int = 0


@dataclass(frozen=True)
class HistorySummary:
    issuers: int
    cutoffs: int
    inserted: int
    already_present: int
    no_outcome: int
    undated: int
    #: Issuers of the strategy universe that stored lineage could not resolve.
    unresolved_issuers: tuple[str, ...] = ()


# -- cutoffs and the strict date rule ---------------------------------------------------


def monthly_cutoffs(tick: datetime, months: int = DEFAULT_MONTHS) -> list[datetime]:
    """The first day of each of the last ``months`` months, 00:00:00Z, oldest first.

    The month of the tick counts as the newest one. Its first day never passes the tick.
    """
    if tick.tzinfo is None or tick.utcoffset() is None:
        raise ValueError("tick must carry a time zone")
    if months < 1:
        raise ValueError(f"months must be at least 1, got {months}")
    utc_tick = tick.astimezone(UTC)
    newest = utc_tick.year * 12 + utc_tick.month - 1
    return [datetime(index // 12, index % 12 + 1, 1, tzinfo=UTC) for index in range(newest - months + 1, newest + 1)]


def end_of_day(day: date) -> datetime:
    """The last instant of a filed day: 23:59:59.999999Z."""
    return datetime.combine(day, time.max, tzinfo=UTC)


def admit_input(cutoff_at: datetime, knowable_at: datetime) -> None:
    """Raise ``LookAheadError`` unless the input is knowable before the cutoff's calendar date."""
    if knowable_at.astimezone(UTC).date() >= cutoff_at.astimezone(UTC).date():
        raise LookAheadError(
            f"input knowable at {knowable_at.isoformat()} is not before the cutoff date {cutoff_at.date().isoformat()}"
        )


# -- per-key filing times ---------------------------------------------------------------


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


def vintage_knowable_at(payload: Mapping[str, Any]) -> Callable[[str, str | None], datetime | None]:
    """The ``knowable_at_of`` callback for ``financial_input_rows``, read from the payload vintage."""
    vintage = payload.get("vintage") or {}

    def knowable_at_of(input_key: str, period_end: str | None) -> datetime | None:
        if period_end is None:
            return _entry_knowable_at(vintage.get(input_key))
        return _entry_knowable_at((vintage.get(f"{input_key}{_PERIOD_SERIES_SUFFIX}") or {}).get(period_end))

    return knowable_at_of


# -- stored inputs ----------------------------------------------------------------------

_LAST_CLOSE_SQL = """
select close, confidence, transaction_time
from staging.market_prices_daily
where symbol = %s and adjust = %s and close is not null
  and trading_date < %s and transaction_time < %s
order by trading_date desc, recorded_at desc
limit 1
"""


def postgres_last_close(connection: psycopg.Connection[Any]) -> PriceReader:
    """The close of the last session before the cutoff date, from the unadjusted daily bars.

    A split-adjusted bar never answers: it is the wrong basis for market value (A6 decision 4).
    When one session has several stored vintages, the latest one wins.
    """

    def last_close(symbol: str, cutoff_at: datetime) -> LastClose | None:
        row = connection.execute(
            _LAST_CLOSE_SQL, (symbol, UNADJUSTED_BARS, cutoff_at.astimezone(UTC).date(), cutoff_at)
        ).fetchone()
        return None if row is None else LastClose(Decimal(row[0]), Decimal(row[1]), row[2])

    return last_close


def load_stored_company_facts(
    connection: psycopg.Connection[Any], cik: int, *, store: Any | None = None
) -> tuple[bytes, dict[str, Any]] | None:
    """The newest stored company-facts document of one CIK, as bytes and parsed JSON.

    The document is cumulative and each datum carries its own filed date, so the newest
    vintage holds every fact of an older one. The bytes come through ``raw_store.get_payload``,
    which verifies their checksum.
    """
    row = connection.execute(
        """
        select raw_fetch_id from raw.capture_source_vintages
        where source_record_id = %s
        order by created_at desc, source_vintage_id desc
        limit 1
        """,
        (company_facts_record_id(cik),),
    ).fetchone()
    if row is None:
        return None
    body = raw_store.get_payload(connection, row[0], store=store)
    return body, json.loads(body)


# -- stored issuer lineage --------------------------------------------------------------

_LATEST_FINANCIAL_SQL = """
select distinct on (p.normalized_payload ->> 'issuer_id') p.normalized_payload, v.source_record_id
from staging.capture_normalized_observations o
join staging.capture_observation_payloads p on p.observation_id = o.observation_id
join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
where o.semantic_type = 'financial-fact'
  and o.parser_version = %s
  and p.normalized_payload ->> 'issuer_id' = any(%s)
order by p.normalized_payload ->> 'issuer_id', o.recorded_at desc, o.knowable_at desc, o.observation_id desc
"""


def latest_financial_observations(
    connection: psycopg.Connection[Any], issuer_ids: Sequence[str], *, parser_version: str = PARSER_VERSION
) -> list[tuple[dict[str, Any], str]]:
    """Per issuer, the payload and the source record id of its newest financial-fact observation."""
    rows = connection.execute(_LATEST_FINANCIAL_SQL, (parser_version, list(issuer_ids))).fetchall()
    return [(dict(payload), str(record_id)) for payload, record_id in rows]


def _ticker_of(listing_id: str, connection: psycopg.Connection[Any] | None, known_at: datetime | None) -> str | None:
    """The listing's ticker. A legacy id spells it. A canonical id resolves through its alias."""
    if is_uuid(listing_id):
        if connection is None or known_at is None:
            return None
        value = alias_of(
            connection, listing_id, "mic-ticker", valid_at=known_at.astimezone(UTC).date(), known_at=known_at
        )
        scheme = "mic-ticker" if value else ""
    else:
        scheme, value = parse_alias(listing_id, "listing")
    if scheme != "mic-ticker" or not value or ":" not in value:
        return None
    return value.split(":", 1)[1]


def _revenue_proxy_signature(payload: Mapping[str, Any]) -> bool:
    """True when the live payload shows gross profit resolved by the revenue proxy.

    Submissions metadata (the SIC) is not stored, and this run makes no vendor call. A proxy
    sets gross profit equal to revenue under one filing, so the live payload of an issuer the
    proxy was approved for shows that pair. The signature is evidence from the issuer's own
    capture, not an allowlist.
    """
    if payload.get("operating_branch") != OperatingBranch.NON_FINANCIAL.value:
        return False
    gross, revenue = payload.get("gross_profit"), payload.get("revenue")
    vintage = payload.get("vintage") or {}
    if gross is None or revenue is None or not vintage.get("revenue"):
        return False
    try:
        same_value = Decimal(str(gross)) == Decimal(str(revenue))
    except InvalidOperation:
        return False
    return same_value and vintage.get("gross_profit") == vintage.get("revenue")


def issuer_from_observation(
    payload: Mapping[str, Any],
    source_record_id: str,
    *,
    connection: psycopg.Connection[Any] | None,
    known_at: datetime | None = None,
) -> HistoryIssuer | None:
    """The projection target one stored financial-fact observation describes, or None.

    None means the lineage cannot name the CIK, the ticker or the operating branch. The caller
    reports the issuer as unresolved. Nothing guesses a missing value.
    """
    digits = source_record_id.removeprefix(_COMPANY_FACTS_PREFIX)
    if not source_record_id.startswith(_COMPANY_FACTS_PREFIX) or not digits.isdigit():
        return None
    listing_id = str(payload["listing_id"])
    ticker = _ticker_of(listing_id, connection, known_at)
    branch = payload.get("operating_branch")
    if ticker is None or branch is None:
        return None
    return HistoryIssuer(
        issuer_id=str(payload["issuer_id"]),
        instrument_id=str(payload.get("instrument_id", "")),
        listing_id=listing_id,
        ticker=ticker,
        cik=int(digits),
        operating_branch=OperatingBranch(branch),
        revenue_proxy_allowed=_revenue_proxy_signature(payload),
    )


def stored_history_issuers(
    connection: psycopg.Connection[Any],
    issuer_ids: Sequence[str],
    *,
    parser_version: str = PARSER_VERSION,
    known_at: datetime | None = None,
) -> list[HistoryIssuer]:
    """The projection targets of the given issuers, resolved from stored capture lineage only."""
    issuers = [
        issuer
        for payload, record_id in latest_financial_observations(connection, issuer_ids, parser_version=parser_version)
        if (issuer := issuer_from_observation(payload, record_id, connection=connection, known_at=known_at))
    ]
    if not issuers:
        return []
    predecessors = predecessor_ciks(
        connection,
        [issuer.listing_id for issuer in issuers],
        {issuer.listing_id: issuer.issuer_id for issuer in issuers},
    )
    resolved: list[HistoryIssuer] = []
    for issuer in issuers:
        predecessor = predecessors.get(issuer.listing_id)
        if predecessor is None or int(predecessor) == issuer.cik:
            resolved.append(issuer)
        else:
            resolved.append(
                dataclasses.replace(issuer, predecessor_cik=int(predecessor), predecessor_signed=predecessor.signed)
            )
    return resolved


# -- projection -------------------------------------------------------------------------


def _work_item(issuer: HistoryIssuer, cutoff_at: datetime) -> CaptureWorkItem:
    """A work item whose identity is the pair (issuer, cutoff). The adapter looks its target up by it."""
    digest = canonical_sha256({"issuer": issuer.issuer_id, "cutoff": cutoff_at.isoformat(), "kind": "strategy-history"})
    return CaptureWorkItem(
        campaign_id=f"capture-campaign:{digest}",
        source_request_id=f"source-request:{digest}",
        schedule_policy_id=f"schedule-policy:{digest}",
    )


def project_issuer_rows(
    issuer: HistoryIssuer,
    cutoffs: Sequence[datetime],
    *,
    load_facts: FactsLoader,
    headcount_extractor: HeadcountExtractor,
    last_close: PriceReader,
    ruleset: ConceptMappingRuleset,
) -> Projection:
    """The input rows of one issuer at each cutoff. Reads only. Writes nothing.

    The adapter runs for the day before the cutoff date. ``build_bundle`` admits a fact filed on
    or before that day, which is the same set as a fact filed before the cutoff date. The
    headcount extractor admits a fact knowable through the end of that same day.
    """
    documents: dict[int, tuple[bytes, dict[str, Any]] | None] = {}

    def fetcher(cik: int, as_of: date, branch: OperatingBranch) -> Any:
        if cik not in documents:
            documents[cik] = load_facts(cik)
        stored = documents[cik]
        if stored is None:
            return None
        body, facts = stored
        return build_bundle(facts, as_of, branch, raw_bytes=body, ruleset=ruleset)

    rows: list[HistoryRow] = []
    no_outcome = undated = 0
    for cutoff_at in cutoffs:
        as_of = cutoff_at.astimezone(UTC).date() - timedelta(days=1)
        item = _work_item(issuer, cutoff_at)
        target = SecTarget(
            cik=issuer.cik,
            cutoff=as_of,
            issuer_id=issuer.issuer_id,
            instrument_id=issuer.instrument_id,
            listing_id=issuer.listing_id,
            operating_branch=issuer.operating_branch,
            revenue_proxy_allowed=issuer.revenue_proxy_allowed,
            predecessor_cik=issuer.predecessor_cik,
            predecessor_signed=issuer.predecessor_signed,
        )
        adapter = SecFinancialFactAdapter(
            {item.work_item_id: target}, fetcher, headcount_extractor=headcount_extractor, ruleset=ruleset
        )
        outcome = adapter.fetch(item)
        projected: list[tuple[str, str, Decimal, datetime, str | None]] = []
        if isinstance(outcome, FetchSuccess):
            if outcome.record is None:
                raise RuntimeError(f"the SEC adapter returned a success without a record for {issuer.issuer_id}")
            payload = outcome.record.payload
            projected = financial_input_rows(
                payload,
                Decimal(str(outcome.confidence)),
                outcome.transaction_time,
                knowable_at_of=vintage_knowable_at(payload),
            )
            expected = financial_input_rows(payload, Decimal(str(outcome.confidence)), outcome.transaction_time)
            undated += len(expected) - len(projected)
        else:
            no_outcome += 1
        for input_key, value, confidence, knowable_at, fiscal_period in projected:
            rows.append(
                _checked(
                    HistoryRow(
                        issuer.issuer_id,
                        cutoff_at,
                        input_key,
                        Decimal(str(value)),
                        confidence,
                        knowable_at,
                        fiscal_period,
                    )
                )
            )
        bar = last_close(issuer.ticker, cutoff_at)
        if bar is not None:
            rows.append(
                _checked(
                    HistoryRow(
                        issuer.issuer_id, cutoff_at, _LAST_CLOSE_KEY, bar.close, bar.confidence, bar.knowable_at, None
                    )
                )
            )
    return Projection(tuple(rows), no_outcome=no_outcome, undated=undated)


def _checked(row: HistoryRow) -> HistoryRow:
    """The row, after the registry check the live writer makes and the strict date check."""
    if not is_registered_input_key(row.input_key):
        raise ValueError(
            f"{row.input_key!r} is not a registered metric (truealpha_contracts.metrics.METRICS); "
            "register it there before projecting it as a strategy input"
        )
    admit_input(row.cutoff_at, row.knowable_at)
    return row


_EXISTING_SQL = """
select cutoff_at, input_key, coalesce(fiscal_period, ''), value, knowable_at
from staging.strategy_backtest_inputs
where issuer_id = %s and cutoff_at = any(%s)
"""

# `recorded_at` is the clock reading of the insert, not the transaction start (`now()`). The
# unique index holds `recorded_at`, so two runs in one transaction that write a changed value for
# one key would otherwise collide on the same `now()`.
_INSERT_SQL = """
insert into staging.strategy_backtest_inputs
    (issuer_id, cutoff_at, input_key, value, confidence, knowable_at, fiscal_period, recorded_at)
values (%s, %s, %s, %s, %s, %s, %s, clock_timestamp())
"""


def _write_new_rows(connection: psycopg.Connection[Any], issuer: HistoryIssuer, rows: Sequence[HistoryRow]) -> int:
    """Insert the rows that the table does not hold yet. Returns the count of skipped rows."""
    cutoffs = sorted({row.cutoff_at for row in rows})
    present = {
        (cutoff_at, input_key, period, Decimal(str(value)), knowable_at)
        for cutoff_at, input_key, period, value, knowable_at in connection.execute(
            _EXISTING_SQL, (issuer.issuer_id, cutoffs)
        ).fetchall()
    }
    fresh = [
        row
        for row in rows
        if (row.cutoff_at, row.input_key, row.fiscal_period or "", row.value, row.knowable_at) not in present
    ]
    with connection.cursor() as cursor:
        cursor.executemany(
            _INSERT_SQL,
            [
                (
                    row.issuer_id,
                    row.cutoff_at,
                    row.input_key,
                    row.value,
                    row.confidence,
                    row.knowable_at,
                    row.fiscal_period,
                )
                for row in fresh
            ],
        )
    return len(rows) - len(fresh)


def run_strategy_history(
    connection: psycopg.Connection[Any],
    *,
    tick: datetime,
    issuers: Sequence[HistoryIssuer],
    load_facts: FactsLoader,
    months: int = DEFAULT_MONTHS,
    headcount_extractor: HeadcountExtractor | None = None,
    last_close: PriceReader | None = None,
    ruleset: ConceptMappingRuleset | None = None,
) -> HistorySummary:
    """Project every issuer over the monthly cutoffs of the tick and insert the new rows.

    The caller owns the transaction. ``tick`` is the run's tick time, never the wall clock, so
    a replay of one tick builds the same cutoffs.
    """
    cutoffs = monthly_cutoffs(tick, months)
    extractor = headcount_extractor if headcount_extractor is not None else PostgresHeadcountExtractor(connection)
    reader = last_close if last_close is not None else postgres_last_close(connection)
    rules = ruleset if ruleset is not None else resolve_ruleset(connection)
    inserted = already_present = no_outcome = undated = 0
    for issuer in issuers:
        projection = project_issuer_rows(
            issuer, cutoffs, load_facts=load_facts, headcount_extractor=extractor, last_close=reader, ruleset=rules
        )
        skipped = _write_new_rows(connection, issuer, projection.rows)
        already_present += skipped
        inserted += len(projection.rows) - skipped
        no_outcome += projection.no_outcome
        undated += projection.undated
    return HistorySummary(
        issuers=len(issuers),
        cutoffs=len(cutoffs),
        inserted=inserted,
        already_present=already_present,
        no_outcome=no_outcome,
        undated=undated,
    )


def project_deployed_history(
    connection: psycopg.Connection[Any],
    *,
    tick: datetime,
    months: int = DEFAULT_MONTHS,
    store: Any | None = None,
) -> HistorySummary:
    """The deployed run: extend the history of every issuer the strategy already consumes.

    The universe is the set of issuers that hold a row in ``staging.strategy_backtest_inputs``.
    The run fails when it resolves no issuer or projects no row, so an empty run cannot report
    success.
    """
    issuer_ids = [
        row[0]
        for row in connection.execute("select distinct issuer_id from staging.strategy_backtest_inputs order by 1")
    ]
    issuers = stored_history_issuers(connection, issuer_ids, known_at=tick)
    if not issuers:
        raise RuntimeError(
            f"no issuer to project: {len(issuer_ids)} issuers hold strategy inputs, none resolves from stored lineage"
        )
    summary = run_strategy_history(
        connection,
        tick=tick,
        issuers=issuers,
        months=months,
        load_facts=lambda cik: load_stored_company_facts(connection, cik, store=store),
    )
    if summary.inserted + summary.already_present == 0:
        raise RuntimeError(
            f"the projector produced no row for {summary.issuers} issuers over {summary.cutoffs} cutoffs"
        )
    resolved = {issuer.issuer_id for issuer in issuers}
    return dataclasses.replace(
        summary, unresolved_issuers=tuple(issuer_id for issuer_id in issuer_ids if issuer_id not in resolved)
    )
