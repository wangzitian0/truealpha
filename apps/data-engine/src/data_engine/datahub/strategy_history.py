"""The point-in-time projector for historical strategy inputs (#1139, M3 track H1, A6).

``staging.strategy_backtest_inputs`` holds one forward snapshot per live tick. No code wrote a
row for a past cutoff, so a backtest over 36 monthly cutoffs had no input history. This module
builds those rows from bytes already stored: the SEC company-facts documents, the headcount
fact table and the daily unadjusted bars. It makes zero vendor calls.

Rules this module enforces:

* Cutoffs are the first calendar day of each of the last N months at 00:00:00Z, plus each
  calendar quarter end of the strategy schedule inside that window (#758). The tick time of the
  run derives them. A quarter-end cutoff is the schedule cutoff the nightly backtest evaluates:
  it closes its day at the schedule's time (23:59:59Z), and the trade fills at the open of the
  next session.
* Strict point in time. A fact is admitted only when its filed date is before the calendar date
  of the cutoff. The row's ``knowable_at`` is the end of the filed day. A fact filed on the
  cutoff date therefore has ``knowable_at > cutoff_at``, and the table CHECK
  ``knowable_at <= cutoff_at`` rejects it. The same rule applies to a quarter-end cutoff at
  23:59:59Z: the close of the quarter-end session itself is not admitted. That is one session
  earlier than the definition's price rule permits. The deviation only removes information.
* Each input carries its own filed date and its own fiscal period, exactly where the live
  writer does. There is no blended value.
* ``last_close`` is the unadjusted close (``adjust = 'none'``, #1131) of the last session
  before the cutoff date. Shares are as filed (A6 decision 4), on the share basis of that close
  (#1257): a split with an ex-date after the count's basis date and on or before the close's
  session multiplies the filed count. The basis date of a cover-page count is its own date; the
  basis date of a statement count is its filing date, because the filing restates it for an
  earlier split. Without this rule, NVDA at 2024-06-30 multiplied a pre-split count by a
  post-split close, its market value fell tenfold, and the strategy selected it. The split events
  come from the two stored price series (``datahub.price_series``). The rule lives in
  ``datahub.share_basis``, which the live strategy seed also uses (#1262).
* Canonical issuers only (#1257). The table holds each issuer under a legacy ``issuer:lei:`` id
  and under its entity UUID. The universe is the set of entities those ids name, and each history
  is written under the entity UUID. No row is written under a legacy id.
* One price symbol per issuer (#1265). The close and the splits come from the first listing
  ticker that holds both stored daily series: the lineage ticker first, then the other tickers
  that the listing-identity cells name, in sorted order. Alphabet is GOOG in lineage, and the
  store holds GOOGL only, so GOOGL prices it. An issuer without such a listing keeps its lineage
  ticker and gets no close.
* The run is idempotent. The unique index of the table includes ``recorded_at``, so a second
  insert of the same fact would duplicate the row. The projector skips a row that exists for
  the same issuer, cutoff, input key and fiscal period with the same value and ``knowable_at``.

The financial inputs come from ``SecFinancialFactAdapter`` over the stored bytes, evaluated for
the day before the cutoff date. That is the same code the live tick runs, so the period gates
(#1114), the revenue proxy gate (#533) and the share staleness bound (#529) apply unchanged. The
rows come from ``strategy_bridge.financial_input_rows``, the function the live writer uses.
"""

from __future__ import annotations

import calendar
import dataclasses
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

import psycopg
from factors.backtest.corporate_actions import SplitEvent
from factors.production_topt import OperatingBranch
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.concept_mapping import ConceptMappingRuleset
from truealpha_contracts.datahub import CaptureWorkItem
from truealpha_contracts.metrics import is_registered_input_key
from truealpha_contracts.strategy import QuarterlyCutoffSchedule

from data_engine import raw_store
from data_engine.datahub.canonical_issuer import stored_entity_of
from data_engine.datahub.price_series import (
    load_daily_series,
    resolve_price_symbol,
    split_events_by_symbol,
    symbols_with_both_series,
)
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
from data_engine.datahub.share_basis import (
    COVER,
    SHARES_KEY,
    STATEMENT,
    UNRECOGNISED,
    on_close_basis,
    share_count_as_of,
    share_count_concepts,
)
from data_engine.datahub.strategy_bridge import financial_input_rows, load_strategy_definition

DEFAULT_MONTHS = 36

#: The ``adjust`` value of the unadjusted daily bars (#1131, ``market_prices.UNADJUSTED``).
#: Market value is the unadjusted close times the as-filed share count (A6 decision 4).
UNADJUSTED_BARS = "none"
#: The ``adjust`` value of the split-adjusted daily bars (``market_prices.SPLIT_ADJUSTED``).
SPLIT_ADJUSTED_BARS = "splits"

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
#: The split events of one symbol, oldest first.
SplitReader = Callable[[str], tuple[SplitEvent, ...]]


@dataclass(frozen=True)
class HistoryIssuer:
    """What the projector needs to project one issuer, read from stored lineage."""

    issuer_id: str
    instrument_id: str
    listing_id: str
    #: The lineage ticker. Every output names the issuer by it.
    ticker: str
    cik: int
    operating_branch: OperatingBranch
    revenue_proxy_allowed: bool = False
    predecessor_cik: int | None = None
    predecessor_signed: bool = False
    #: The listing ticker whose stored daily series price the issuer (#1265, ``resolve_price_symbol``).
    #: None means that no listing holds both series.
    price_symbol: str | None = None

    @property
    def series_symbol(self) -> str:
        """The symbol that the price readers look up: the price symbol, else the lineage ticker."""
        return self.price_symbol or self.ticker


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
    #: Share rows put on the share basis of their close by a split after the count's basis date (#1257).
    split_adjusted: int = 0
    #: Share counts classed as cover-page counts: their basis date is their own date.
    share_basis_cover: int = 0
    #: Share counts classed as statement figures: their basis date is their filing date.
    share_basis_statement: int = 0
    #: Share counts that match no stored entry: they fall back to their filing date, which can under-adjust.
    share_basis_unrecognised: int = 0


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
    #: Old-generation ids that name a projected entity. Their history is written under the entity UUID.
    merged_ids: int = 0
    #: Share rows put on the share basis of their close by a split after the count's basis date (#1257).
    split_adjusted: int = 0
    #: Share counts classed as cover-page counts (basis date: their own date).
    share_basis_cover: int = 0
    #: Share counts classed as statement figures (basis date: their filing date).
    share_basis_statement: int = 0
    #: Share counts that match no stored entry (basis date: their filing date, which can under-adjust).
    share_basis_unrecognised: int = 0


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


def history_cutoffs(tick: datetime, months: int, schedule: QuarterlyCutoffSchedule) -> list[datetime]:
    """The monthly cutoffs of the tick and the schedule's quarter ends from the oldest of them to the tick."""
    monthly = monthly_cutoffs(tick, months)
    return sorted(set(monthly) | set(quarter_end_cutoffs(schedule, start=monthly[0], end=tick)))


_QUARTER_END_MONTHS = (3, 6, 9, 12)


def quarter_end_cutoffs(schedule: QuarterlyCutoffSchedule, *, start: datetime, end: datetime) -> list[datetime]:
    """The calendar quarter ends of the schedule in ``[start, end]``, oldest first.

    Each cutoff is the last day of a quarter at the schedule's time of day (23:59:59Z, enforced
    by ``QuarterlyCutoffSchedule``). ``first_cutoff_at`` anchors the LIVE schedule only: it says
    when live quarterly decisions begin. It does not bound a historical window, so a backtest
    can evaluate quarter ends before it.
    """
    for name, value in (("start", start), ("end", end)):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must carry a time zone")
    clock = schedule.first_cutoff_at.astimezone(UTC).timetz()
    first, last = start.astimezone(UTC), end.astimezone(UTC)
    cutoffs: list[datetime] = []
    for year in range(first.year, last.year + 1):
        for month in _QUARTER_END_MONTHS:
            day = date(year, month, calendar.monthrange(year, month)[1])
            cutoff = datetime.combine(day, clock)
            if first <= cutoff <= last:
                cutoffs.append(cutoff)
    return cutoffs


def is_schedule_cutoff(value: datetime, schedule: QuarterlyCutoffSchedule) -> bool:
    """True when ``value`` is a calendar quarter end at the schedule's time of day."""
    if value.tzinfo is None or value.utcoffset() is None:
        return False
    utc = value.astimezone(UTC)
    return quarter_end_cutoffs(schedule, start=utc, end=utc) == [utc]


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


def postgres_split_events(connection: psycopg.Connection[Any], symbols: Sequence[str]) -> SplitReader:
    """The split events of each symbol, derived from its two stored daily series, before any projection.

    Raise ONE ``SplitDerivationError`` that names every symbol whose series disagree: a share count
    cannot be put on the basis of a close when the splits between them are unknown.
    """
    unadjusted = load_daily_series(connection, symbols, adjust=UNADJUSTED_BARS)
    split_adjusted = load_daily_series(connection, symbols, adjust=SPLIT_ADJUSTED_BARS)
    events = split_events_by_symbol(symbols, unadjusted, split_adjusted)
    return lambda symbol: events.get(symbol, ())


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
select distinct on (named.entity_id) named.entity_id, p.normalized_payload, v.source_record_id
from unnest(%s::text[], %s::text[]) as named (issuer_id, entity_id)
join staging.capture_observation_payloads p on p.normalized_payload ->> 'issuer_id' = named.issuer_id
join staging.capture_normalized_observations o on o.observation_id = p.observation_id
join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
where o.semantic_type = 'financial-fact'
  and o.parser_version = %s
order by named.entity_id, o.recorded_at desc, o.knowable_at desc, o.observation_id desc
"""


def latest_financial_observations(
    connection: psycopg.Connection[Any],
    ids_by_entity: Mapping[str, Sequence[str]],
    *,
    parser_version: str = PARSER_VERSION,
) -> dict[str, tuple[dict[str, Any], str]]:
    """Per entity, the payload and source record id of the newest financial-fact observation
    recorded under any of the entity's ids (#1257: a legacy id and the entity UUID name one issuer)."""
    pairs = [(issuer_id, entity_id) for entity_id, ids in ids_by_entity.items() for issuer_id in ids]
    rows = connection.execute(
        _LATEST_FINANCIAL_SQL,
        ([issuer_id for issuer_id, _ in pairs], [entity_id for _, entity_id in pairs], parser_version),
    ).fetchall()
    return {str(entity_id): (dict(payload), str(record_id)) for entity_id, payload, record_id in rows}


# The live seed reads the same cells for one capture run (strategy_bridge). This read spans every run.
_ENTITY_LISTING_TICKERS_SQL = """
select distinct named.entity_id, p.normalized_payload ->> 'ticker'
from unnest(%s::text[], %s::text[]) as named (issuer_id, entity_id)
join staging.capture_observation_payloads p on p.normalized_payload ->> 'issuer_id' = named.issuer_id
join staging.capture_normalized_observations o on o.observation_id = p.observation_id
where o.semantic_type = 'listing-identity'
  and o.knowable_at <= %s
  and p.normalized_payload ->> 'ticker' is not null
"""


def listing_tickers(
    connection: psycopg.Connection[Any], ids_by_entity: Mapping[str, Sequence[str]], *, known_at: datetime
) -> dict[str, tuple[str, ...]]:
    """Per entity, the tickers of its listings that the stored listing-identity cells name (#1265).

    A cell names the issuer id that its capture used: the entity UUID, or a legacy id before the
    identity unification. So the read covers every id of the entity. A cell knowable after
    ``known_at`` is not read. Alphabet's cells name GOOG and GOOGL.
    """
    pairs = [(issuer_id, entity_id) for entity_id, ids in ids_by_entity.items() for issuer_id in ids]
    rows = connection.execute(
        _ENTITY_LISTING_TICKERS_SQL,
        ([issuer_id for issuer_id, _ in pairs], [entity_id for _, entity_id in pairs], known_at),
    ).fetchall()
    found: dict[str, set[str]] = {}
    for entity_id, ticker in rows:
        found.setdefault(str(entity_id), set()).add(str(ticker))
    return {entity_id: tuple(sorted(tickers)) for entity_id, tickers in sorted(found.items())}


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


@dataclass(frozen=True)
class CanonicalIssuers:
    """Issuer ids resolved to their canonical entity (#1257).

    ``staging.strategy_backtest_inputs`` holds each issuer under two id generations: the legacy
    ``issuer:lei:`` id of the old live seeds and the entity UUID of the seeds since the identity
    unification. Old rows never leave the append-only table. Readers use the canonical ids only.
    """

    #: One projection target per canonical entity, keyed by the entity UUID.
    issuers: tuple[HistoryIssuer, ...]
    #: Every id that is itself a canonical entity id, with or without resolved lineage.
    canonical_ids: frozenset[str]
    #: Each other id and the entity it names. Readers ignore its rows; the projector never writes it.
    merged: Mapping[str, str]
    #: Ids that name no entity, and the ids of an entity whose lineage names no CIK, ticker or branch.
    unresolved: tuple[str, ...]


def canonical_history_issuers(
    connection: psycopg.Connection[Any],
    issuer_ids: Sequence[str],
    *,
    known_at: datetime,
    parser_version: str = PARSER_VERSION,
) -> CanonicalIssuers:
    """The canonical projection targets of the given ids, resolved from stored lineage only.

    Each id resolves to its entity through ``stored_entity_of``: it reads the aliases, never
    mints, and refuses a UUID that is not an entity of the store. The lineage of an entity is the
    newest financial-fact observation recorded under any of its ids, so an issuer captured only
    under its legacy id still resolves, under its UUID. Each issuer carries its price symbol
    (#1265), so the projector and the backtest read the same series.
    """
    if known_at.tzinfo is None or known_at.utcoffset() is None:
        raise ValueError("known_at must carry a time zone")
    as_of = known_at.astimezone(UTC).date()
    ids_by_entity: dict[str, list[str]] = {}
    unresolved: list[str] = []
    for issuer_id in sorted(set(issuer_ids)):
        named = stored_entity_of(connection, issuer_id, as_of=as_of, known_at=known_at)
        if named is None:
            unresolved.append(issuer_id)
        else:
            ids_by_entity.setdefault(str(named), []).append(issuer_id)
    observations = latest_financial_observations(
        connection,
        {entity: sorted({entity, *ids}) for entity, ids in ids_by_entity.items()},
        parser_version=parser_version,
    )
    issuers: list[HistoryIssuer] = []
    for entity, ids in sorted(ids_by_entity.items()):
        found = observations.get(entity)
        issuer = None if found is None else issuer_from_observation(*found, connection=connection, known_at=known_at)
        if issuer is None:
            unresolved.extend(ids)
        else:
            issuers.append(dataclasses.replace(issuer, issuer_id=entity))
    resolved = {issuer.issuer_id for issuer in issuers}
    targets = _with_predecessors(connection, issuers, ids_by_entity)
    return CanonicalIssuers(
        issuers=tuple(_with_price_symbols(connection, targets, ids_by_entity, known_at=known_at)),
        canonical_ids=frozenset(entity for entity, ids in ids_by_entity.items() if entity in ids),
        merged={issuer_id: entity for entity in resolved for issuer_id in ids_by_entity[entity] if issuer_id != entity},
        unresolved=tuple(sorted(unresolved)),
    )


class ConflictingPredecessorError(ValueError):
    """Two signed registry rows name different predecessor CIKs for one issuer."""


def _with_predecessors(
    connection: psycopg.Connection[Any], issuers: Sequence[HistoryIssuer], ids_by_entity: Mapping[str, Sequence[str]]
) -> list[HistoryIssuer]:
    """Each issuer with the predecessor CIK that ``predecessor_ciks`` resolves for its listing.

    The signed registry (``staging.issuer_cik_predecessors``) is keyed by the id it was written
    under, and its rows predate the entity UUIDs: ExxonMobil's row names ``issuer:lei:``. So each
    of the entity's ids is presented in turn, and a signed resolution under any of them wins.
    """
    if not issuers:
        return []
    listings = [issuer.listing_id for issuer in issuers]
    candidates = {
        issuer.listing_id: sorted({issuer.issuer_id, *ids_by_entity.get(issuer.issuer_id, ())}) for issuer in issuers
    }
    found: dict[str, Any] = {}
    signed: dict[str, dict[int, str]] = {}
    for index in range(max(len(ids) for ids in candidates.values())):
        presented = {listing: ids[index] for listing, ids in candidates.items() if index < len(ids)}
        for listing, resolution in predecessor_ciks(connection, listings, presented).items():
            if resolution.signed:
                signed.setdefault(listing, {}).setdefault(int(resolution), presented[listing])
            current = found.get(listing)
            if current is None or (resolution.signed and not current.signed):
                found[listing] = resolution
    conflicts = {listing: named for listing, named in signed.items() if len(named) > 1}
    if conflicts:
        issuer_of = {issuer.listing_id: issuer.issuer_id for issuer in issuers}
        raise ConflictingPredecessorError(
            "signed predecessor rows disagree: "
            + "; ".join(
                f"{issuer_of[listing]}: " + ", ".join(f"CIK {cik} under {named[cik]}" for cik in sorted(named))
                for listing, named in sorted(conflicts.items())
            )
        )
    resolved: list[HistoryIssuer] = []
    for issuer in issuers:
        predecessor = found.get(issuer.listing_id)
        if predecessor is None or int(predecessor) == issuer.cik:
            resolved.append(issuer)
        else:
            resolved.append(
                dataclasses.replace(issuer, predecessor_cik=int(predecessor), predecessor_signed=predecessor.signed)
            )
    return resolved


def _with_price_symbols(
    connection: psycopg.Connection[Any],
    issuers: Sequence[HistoryIssuer],
    ids_by_entity: Mapping[str, Sequence[str]],
    *,
    known_at: datetime,
) -> list[HistoryIssuer]:
    """Each issuer with the price symbol that ``resolve_price_symbol`` gives for its listings (#1265).

    The listing tickers come from the listing-identity cells under every id of the entity. The
    priced symbols come from the stored series of the lineage tickers and those listing tickers.
    """
    if not issuers:
        return []
    tickers = listing_tickers(
        connection,
        {issuer.issuer_id: sorted({issuer.issuer_id, *ids_by_entity.get(issuer.issuer_id, ())}) for issuer in issuers},
        known_at=known_at,
    )
    candidates = {issuer.ticker for issuer in issuers} | {ticker for names in tickers.values() for ticker in names}
    priced = symbols_with_both_series(connection, sorted(candidates))
    return [
        dataclasses.replace(
            issuer, price_symbol=resolve_price_symbol(issuer.ticker, tickers.get(issuer.issuer_id, ()), priced)
        )
        for issuer in issuers
    ]


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
    split_events: SplitReader,
    ruleset: ConceptMappingRuleset,
) -> Projection:
    """The input rows of one issuer at each cutoff. Reads only. Writes nothing.

    The adapter runs for the day before the cutoff date. ``build_bundle`` admits a fact filed on
    or before that day, which is the same set as a fact filed before the cutoff date. The
    headcount extractor admits a fact knowable through the end of that same day. The close and
    the splits are those of the issuer's ``series_symbol`` (#1265).
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
    no_outcome = undated = split_adjusted = 0
    splits: tuple[SplitEvent, ...] | None = None
    cover_concepts, statement_concepts = share_count_concepts(ruleset)
    bases = {COVER: 0, STATEMENT: 0, UNRECOGNISED: 0}
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
        bar = last_close(issuer.series_symbol, cutoff_at)
        for input_key, value, confidence, knowable_at, fiscal_period in projected:
            row = HistoryRow(
                issuer.issuer_id, cutoff_at, input_key, Decimal(str(value)), confidence, knowable_at, fiscal_period
            )
            if input_key == SHARES_KEY and fiscal_period is None and bar is not None:
                if splits is None:
                    splits = split_events(issuer.series_symbol)
                basis = share_count_as_of(
                    (payload.get("vintage") or {}).get(SHARES_KEY),
                    row.value,
                    [stored[1] for stored in documents.values() if stored is not None],
                    cover_concepts=cover_concepts,
                    statement_concepts=statement_concepts,
                    filed_fallback=row.knowable_at.astimezone(UTC).date(),
                )
                bases[basis.kind] += 1
                on_basis = _on_close_basis(row, bar, splits, as_of=basis.as_of)
                split_adjusted += on_basis is not row
                row = on_basis
            rows.append(_checked(row))
        if bar is not None:
            rows.append(
                _checked(
                    HistoryRow(
                        issuer.issuer_id, cutoff_at, _LAST_CLOSE_KEY, bar.close, bar.confidence, bar.knowable_at, None
                    )
                )
            )
    return Projection(
        tuple(rows),
        no_outcome=no_outcome,
        undated=undated,
        split_adjusted=split_adjusted,
        share_basis_cover=bases[COVER],
        share_basis_statement=bases[STATEMENT],
        share_basis_unrecognised=bases[UNRECOGNISED],
    )


def _on_close_basis(shares: HistoryRow, bar: LastClose, splits: Sequence[SplitEvent], *, as_of: date) -> HistoryRow:
    """The share row on the share basis of ``bar``, or the row itself when no split lies between.

    The filed count is on the basis of ``as_of`` (``share_count_as_of``). ``on_close_basis`` applies
    every split after that date and on or before the close's session. The session is the UTC date
    of the close's knowable time, which is before the cutoff date.
    """
    basis = on_close_basis(
        shares.value,
        shares.knowable_at,
        basis_date=as_of,
        close_session=bar.knowable_at.astimezone(UTC).date(),
        close_knowable_at=bar.knowable_at,
        splits=splits,
    )
    if basis.factor == 1:
        return shares
    return dataclasses.replace(shares, value=basis.value, knowable_at=basis.knowable_at)


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
    split_events: SplitReader | None = None,
    ruleset: ConceptMappingRuleset | None = None,
    schedule: QuarterlyCutoffSchedule | None = None,
) -> HistorySummary:
    """Project every issuer over the cutoffs of the tick and insert the new rows.

    The cutoffs are the monthly cutoffs of the tick plus the quarter ends of the strategy
    schedule from the oldest monthly cutoff to the tick. The caller owns the transaction.
    ``tick`` is the run's tick time, never the wall clock, so a replay of one tick builds the
    same cutoffs.
    """
    cutoffs = history_cutoffs(tick, months, schedule if schedule is not None else load_strategy_definition().schedule)
    extractor = headcount_extractor if headcount_extractor is not None else PostgresHeadcountExtractor(connection)
    reader = last_close if last_close is not None else postgres_last_close(connection)
    splits = (
        split_events
        if split_events is not None
        else postgres_split_events(connection, [issuer.series_symbol for issuer in issuers])
    )
    rules = ruleset if ruleset is not None else resolve_ruleset(connection)
    inserted = already_present = no_outcome = undated = split_adjusted = 0
    basis_cover = basis_statement = basis_unrecognised = 0
    for issuer in issuers:
        projection = project_issuer_rows(
            issuer,
            cutoffs,
            load_facts=load_facts,
            headcount_extractor=extractor,
            last_close=reader,
            split_events=splits,
            ruleset=rules,
        )
        skipped = _write_new_rows(connection, issuer, projection.rows)
        already_present += skipped
        inserted += len(projection.rows) - skipped
        no_outcome += projection.no_outcome
        undated += projection.undated
        split_adjusted += projection.split_adjusted
        basis_cover += projection.share_basis_cover
        basis_statement += projection.share_basis_statement
        basis_unrecognised += projection.share_basis_unrecognised
    return HistorySummary(
        issuers=len(issuers),
        cutoffs=len(cutoffs),
        inserted=inserted,
        already_present=already_present,
        no_outcome=no_outcome,
        undated=undated,
        split_adjusted=split_adjusted,
        share_basis_cover=basis_cover,
        share_basis_statement=basis_statement,
        share_basis_unrecognised=basis_unrecognised,
    )


def project_deployed_history(
    connection: psycopg.Connection[Any],
    *,
    tick: datetime,
    months: int = DEFAULT_MONTHS,
    store: Any | None = None,
) -> HistorySummary:
    """The deployed run: extend the history of every issuer the strategy already consumes.

    The universe is the canonical entities of the ids that hold a row in
    ``staging.strategy_backtest_inputs`` (#1257). The table holds each issuer under a legacy id
    and under its entity UUID, so the run resolves every id to its entity and projects one
    history per entity, under the UUID. It never writes a row under a legacy id. The run fails
    when it resolves no issuer or projects no row, so an empty run cannot report success.
    """
    issuer_ids = [
        row[0]
        for row in connection.execute("select distinct issuer_id from staging.strategy_backtest_inputs order by 1")
    ]
    canonical = canonical_history_issuers(connection, issuer_ids, known_at=tick)
    if not canonical.issuers:
        raise RuntimeError(
            f"no issuer to project: {len(issuer_ids)} issuers hold strategy inputs, none resolves from stored lineage"
        )
    summary = run_strategy_history(
        connection,
        tick=tick,
        issuers=list(canonical.issuers),
        months=months,
        load_facts=lambda cik: load_stored_company_facts(connection, cik, store=store),
    )
    if summary.inserted + summary.already_present == 0:
        raise RuntimeError(
            f"the projector produced no row for {summary.issuers} issuers over {summary.cutoffs} cutoffs"
        )
    return dataclasses.replace(summary, unresolved_issuers=canonical.unresolved, merged_ids=len(canonical.merged))
