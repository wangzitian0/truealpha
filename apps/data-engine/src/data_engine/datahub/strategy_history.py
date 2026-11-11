from __future__ import annotations

import calendar
import dataclasses
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, NamedTuple

import psycopg
from factors.backtest.corporate_actions import SplitEvent
from factors.confidence import evaluate_cell_confidence
from factors.production_topt import OperatingBranch
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.concept_mapping import ConceptMappingRuleset
from truealpha_contracts.confidence import ContinuousConfidencePolicy, SourceConfidenceEvidence
from truealpha_contracts.datahub import CaptureWorkItem
from truealpha_contracts.metrics import is_registered_input_key
from truealpha_contracts.strategy import QuarterlyCutoffSchedule

from data_engine import raw_store
from data_engine.datahub.canonical_issuer import (
    ConflictingPredecessorError as ConflictingPredecessorError,
)
from data_engine.datahub.canonical_issuer import stored_entity_of
from data_engine.datahub.input_cells import InputCell, financial_input_rows, vintage_knowable_at
from data_engine.datahub.price_series import load_daily_series, split_events_by_symbol
from data_engine.datahub.primary_listing import choose_primary_listings
from data_engine.datahub.production_topt.concept_mapping import resolve_ruleset
from data_engine.datahub.production_topt.executor import FetchSuccess
from data_engine.datahub.production_topt.headcount import PostgresHeadcountExtractor
from data_engine.datahub.production_topt.issuer_registry import UNAVAILABLE_PAYLOAD_KEY
from data_engine.datahub.production_topt.parser_identity import PARSER_VERSION
from data_engine.datahub.production_topt.sec_financial_adapter import (
    HeadcountExtractor,
    ListingRatioReader,
    SecFinancialFactAdapter,
    SecTarget,
    build_bundle,
    company_facts_record_id,
    predecessor_ciks,
)
from data_engine.datahub.production_topt.source_registrations import (
    last_settled_accession_date,
    source_reliability_prior,
)
from data_engine.datahub.resolve_coordinates import alias_of, is_uuid, parse_alias
from data_engine.datahub.share_basis import (
    COVER,
    SHARES_KEY,
    STATEMENT,
    UNRECOGNISED,
    Unavailable,
    listing_units_per_share_of,
    on_close_basis,
    share_count_as_of,
    share_count_concepts,
)
from data_engine.datahub.standards.listing_unit_ratio import CoverRatioReader
from data_engine.datahub.strategy_bridge import load_strategy_definition

DEFAULT_MONTHS = 36


UNADJUSTED_BARS = "none"

SPLIT_ADJUSTED_BARS = "splits"

_COMPANY_FACTS_PREFIX = "companyfacts:CIK"


_LAST_CLOSE_KEY = "last_close"

FactsLoader = Callable[[int], tuple[bytes, dict[str, Any]] | None]


class LookAheadError(ValueError):
    """An input is knowable on or after the calendar date of the cutoff."""


class LastClose(NamedTuple):
    close: Decimal
    confidence: Decimal
    knowable_at: datetime


PriceReader = Callable[[str, datetime], LastClose | None]

SplitReader = Callable[[str], tuple[SplitEvent, ...]]


@dataclass(frozen=True)
class HistoryIssuer:
    issuer_id: str
    instrument_id: str
    listing_id: str
    ticker: str
    cik: int
    operating_branch: OperatingBranch
    predecessor_cik: int | None = None
    predecessor_signed: bool = False


@dataclass(frozen=True)
class HistoryRow:
    issuer_id: str
    cutoff_at: datetime
    input_key: str
    value: Decimal
    confidence: Decimal
    knowable_at: datetime
    fiscal_period: str | None


@dataclass(frozen=True)
class Projection:
    rows: tuple[HistoryRow, ...]

    no_outcome: int = 0

    undated: int = 0

    split_adjusted: int = 0

    share_basis_cover: int = 0

    share_basis_statement: int = 0

    share_basis_unrecognised: int = 0


@dataclass(frozen=True)
class HistorySummary:
    issuers: int
    cutoffs: int
    inserted: int
    already_present: int
    no_outcome: int
    undated: int

    unresolved_issuers: tuple[str, ...] = ()

    merged_ids: int = 0

    split_adjusted: int = 0

    share_basis_cover: int = 0

    share_basis_statement: int = 0

    share_basis_unrecognised: int = 0


def monthly_cutoffs(tick: datetime, months: int = DEFAULT_MONTHS) -> list[datetime]:
    if tick.tzinfo is None or tick.utcoffset() is None:
        raise ValueError("tick must carry a time zone")
    if months < 1:
        raise ValueError(f"months must be at least 1, got {months}")
    utc_tick = tick.astimezone(UTC)
    newest = utc_tick.year * 12 + utc_tick.month - 1
    return [datetime(index // 12, index % 12 + 1, 1, tzinfo=UTC) for index in range(newest - months + 1, newest + 1)]


def history_cutoffs(tick: datetime, months: int, schedule: QuarterlyCutoffSchedule) -> list[datetime]:
    monthly = monthly_cutoffs(tick, months)
    return sorted(set(monthly) | set(quarter_end_cutoffs(schedule, start=monthly[0], end=tick)))


_QUARTER_END_MONTHS = (3, 6, 9, 12)


def quarter_end_cutoffs(schedule: QuarterlyCutoffSchedule, *, start: datetime, end: datetime) -> list[datetime]:
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
    if value.tzinfo is None or value.utcoffset() is None:
        return False
    utc = value.astimezone(UTC)
    return quarter_end_cutoffs(schedule, start=utc, end=utc) == [utc]


def admit_input(cutoff_at: datetime, knowable_at: datetime) -> None:
    if knowable_at.astimezone(UTC).date() >= cutoff_at.astimezone(UTC).date():
        raise LookAheadError(
            f"input knowable at {knowable_at.isoformat()} is not before the cutoff date {cutoff_at.date().isoformat()}"
        )


_LAST_CLOSE_SQL = """
select close, transaction_time
from staging.market_prices_daily
where symbol = %s and adjust = %s and close is not null
  and trading_date < %s and transaction_time < %s
order by trading_date desc, recorded_at desc
limit 1
"""


_CONFIDENCE_POLICY = ContinuousConfidencePolicy()


def _twelve_data_bar_confidence(symbol: str) -> Decimal:
    safe_symbol = "".join(c if c.isalnum() or c in "._:/@+-" else "-" for c in symbol).strip("-") or "symbol"
    return evaluate_cell_confidence(
        case_id=f"history-close:{safe_symbol}",
        sources=(
            SourceConfidenceEvidence(
                provider_id="origin:twelve-data:v1",
                origin_group_id="lineage:twelve-data",
                independence_weight=Decimal("1"),
                reliability_prior=source_reliability_prior("origin:twelve-data:v1"),
                successful_outcome_mass=Decimal("0"),
                failed_outcome_mass=Decimal("0"),
                freshness=Decimal("1"),
                sample_conformance=Decimal("1"),
                transport_integrity=Decimal("1"),
                evidence_ids=(f"history-close:{safe_symbol}",),
                reason_codes=("reason:single_origin",),
            ),
        ),
        agreement=Decimal("1"),
        semantic_mapping_quality=Decimal("1"),
        lineage_completeness=Decimal("1"),
        required_component_completeness=Decimal("1"),
        policy=_CONFIDENCE_POLICY,
    ).confidence


def postgres_last_close(connection: psycopg.Connection[Any]) -> PriceReader:

    def last_close(symbol: str, cutoff_at: datetime) -> LastClose | None:
        row = connection.execute(
            _LAST_CLOSE_SQL, (symbol, UNADJUSTED_BARS, cutoff_at.astimezone(UTC).date(), cutoff_at)
        ).fetchone()
        return None if row is None else LastClose(Decimal(row[0]), _twelve_data_bar_confidence(symbol), row[1])

    return last_close


def postgres_split_events(connection: psycopg.Connection[Any], symbols: Sequence[str]) -> SplitReader:
    unadjusted = load_daily_series(connection, symbols, adjust=UNADJUSTED_BARS)
    split_adjusted = load_daily_series(connection, symbols, adjust=SPLIT_ADJUSTED_BARS)
    events = split_events_by_symbol(symbols, unadjusted, split_adjusted)
    return lambda symbol: events.get(symbol, ())


def load_stored_company_facts(
    connection: psycopg.Connection[Any], cik: int, *, store: Any | None = None
) -> tuple[bytes, dict[str, Any]] | None:
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


_LATEST_FINANCIAL_SQL = """
select entity_id, normalized_payload, source_record_id
from (
    select distinct on (named.entity_id, p.normalized_payload ->> 'listing_id')
           named.entity_id, p.normalized_payload, v.source_record_id,
           p.normalized_payload ->> 'listing_id' as listing_id, o.recorded_at, o.knowable_at
    from unnest(%s::text[], %s::text[]) as named (issuer_id, entity_id)
    join staging.capture_observation_payloads p on p.normalized_payload ->> 'issuer_id' = named.issuer_id
    join staging.capture_normalized_observations o on o.observation_id = p.observation_id
    join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
    where o.semantic_type = 'financial-fact'
      and o.parser_version = %s
      -- #1323: an observation of an issuer the registry could not classify names a placeholder branch.
      and p.normalized_payload ->> %s is distinct from 'true'
    order by named.entity_id, p.normalized_payload ->> 'listing_id',
             o.recorded_at desc, o.knowable_at desc, o.observation_id desc
) newest
order by entity_id, recorded_at desc, knowable_at desc, listing_id
"""


def latest_financial_observations(
    connection: psycopg.Connection[Any],
    ids_by_entity: Mapping[str, Sequence[str]],
    *,
    known_at: datetime,
    parser_version: str = PARSER_VERSION,
) -> dict[str, tuple[dict[str, Any], str]]:
    pairs = [(issuer_id, entity_id) for entity_id, ids in ids_by_entity.items() for issuer_id in ids]
    rows = connection.execute(
        _LATEST_FINANCIAL_SQL,
        (
            [issuer_id for issuer_id, _ in pairs],
            [entity_id for _, entity_id in pairs],
            parser_version,
            UNAVAILABLE_PAYLOAD_KEY,
        ),
    ).fetchall()
    candidates: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for entity_id, payload, record_id in rows:
        candidates.setdefault(str(entity_id), []).append((dict(payload), str(record_id)))
    chosen = choose_primary_listings(
        connection,
        {entity_id: [str(item[0]["listing_id"]) for item in found] for entity_id, found in candidates.items()},
        known_at=known_at,
    )

    return {
        entity_id: next(item for item in found if str(item[0]["listing_id"]) == chosen[entity_id])
        for entity_id, found in candidates.items()
        if chosen[entity_id] is not None
    }


def _ticker_of(listing_id: str, connection: psycopg.Connection[Any] | None, known_at: datetime | None) -> str | None:
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


def issuer_from_observation(
    payload: Mapping[str, Any],
    source_record_id: str,
    *,
    connection: psycopg.Connection[Any] | None,
    known_at: datetime | None = None,
) -> HistoryIssuer | None:
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
    )


@dataclass(frozen=True)
class CanonicalIssuers:
    issuers: tuple[HistoryIssuer, ...]

    canonical_ids: frozenset[str]

    merged: Mapping[str, str]

    unresolved: tuple[str, ...]


def canonical_history_issuers(
    connection: psycopg.Connection[Any],
    issuer_ids: Sequence[str],
    *,
    known_at: datetime,
    parser_version: str = PARSER_VERSION,
) -> CanonicalIssuers:
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
        known_at=known_at,
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
    return CanonicalIssuers(
        issuers=tuple(_with_predecessors(connection, issuers, ids_by_entity)),
        canonical_ids=frozenset(entity for entity, ids in ids_by_entity.items() if entity in ids),
        merged={issuer_id: entity for entity in resolved for issuer_id in ids_by_entity[entity] if issuer_id != entity},
        unresolved=tuple(sorted(unresolved)),
    )


def _with_predecessors(
    connection: psycopg.Connection[Any], issuers: Sequence[HistoryIssuer], ids_by_entity: Mapping[str, Sequence[str]]
) -> list[HistoryIssuer]:
    if not issuers:
        return []
    listings = [issuer.listing_id for issuer in issuers]
    issuer_by_listing = {issuer.listing_id: issuer.issuer_id for issuer in issuers}
    candidates = {
        issuer.listing_id: sorted({issuer.issuer_id, *ids_by_entity.get(issuer.issuer_id, ())}) for issuer in issuers
    }
    found = predecessor_ciks(connection, listings, issuer_by_listing, candidate_ids_by_listing=candidates)
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


def _work_item(issuer: HistoryIssuer, cutoff_at: datetime) -> CaptureWorkItem:
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
    listing_ratio_reader: ListingRatioReader | None = None,
) -> Projection:
    documents: dict[int, tuple[bytes, dict[str, Any]] | None] = {}
    ratio_reader = listing_ratio_reader if listing_ratio_reader is not None else CoverRatioReader()

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
        as_of = last_settled_accession_date(cutoff_at)
        item = _work_item(issuer, cutoff_at)
        target = SecTarget(
            cik=issuer.cik,
            cutoff=as_of,
            issuer_id=issuer.issuer_id,
            instrument_id=issuer.instrument_id,
            listing_id=issuer.listing_id,
            operating_branch=issuer.operating_branch,
            predecessor_cik=issuer.predecessor_cik,
            predecessor_signed=issuer.predecessor_signed,
            ticker=issuer.ticker,
        )
        adapter = SecFinancialFactAdapter(
            {item.work_item_id: target},
            fetcher,
            headcount_extractor=headcount_extractor,
            ruleset=ruleset,
            listing_ratio_reader=ratio_reader,
        )
        outcome = adapter.fetch(item)
        projected: list[InputCell] = []
        if isinstance(outcome, FetchSuccess):
            if outcome.record is None:
                raise RuntimeError(f"the SEC adapter returned a success without a record for {issuer.issuer_id}")
            payload = outcome.record.payload
            projected = financial_input_rows(
                payload,
                None,
                outcome.transaction_time,
                knowable_at_of=vintage_knowable_at(payload),
            )
            expected = financial_input_rows(payload, None, outcome.transaction_time)
            undated += len(expected) - len(projected)
        else:
            no_outcome += 1
        bar = last_close(issuer.ticker, cutoff_at)
        for input_key, value, _unit, confidence, knowable_at, fiscal_period in projected:
            row = HistoryRow(issuer.issuer_id, cutoff_at, input_key, value, confidence, knowable_at, fiscal_period)
            if input_key == SHARES_KEY and fiscal_period is None:
                filed_date = row.knowable_at.astimezone(UTC).date()
                if bar is not None:
                    if splits is None:
                        splits = split_events(issuer.ticker)
                    basis = share_count_as_of(
                        (payload.get("vintage") or {}).get(SHARES_KEY),
                        row.value,
                        [stored[1] for stored in documents.values() if stored is not None],
                        cover_concepts=cover_concepts,
                        statement_concepts=statement_concepts,
                        filed_fallback=filed_date,
                    )
                    bases[basis.kind] += 1
                    basis_as_of = basis.as_of
                else:
                    basis_as_of = filed_date
                on_basis = _on_close_basis(
                    row,
                    bar,
                    splits or (),
                    as_of=basis_as_of,
                    listing_units_per_share=listing_units_per_share_of(payload),
                )
                if on_basis is None:
                    continue
                row, adjusted = on_basis
                split_adjusted += adjusted
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


def _on_close_basis(
    shares: HistoryRow,
    bar: LastClose | None,
    splits: Sequence[SplitEvent],
    *,
    as_of: date,
    listing_units_per_share: Decimal | None,
) -> tuple[HistoryRow, bool] | None:
    close_session = as_of if bar is None else bar.knowable_at.astimezone(UTC).date()
    close_knowable_at = shares.knowable_at if bar is None else bar.knowable_at
    basis = on_close_basis(
        shares.value,
        shares.knowable_at,
        basis_date=as_of,
        close_session=close_session,
        close_knowable_at=close_knowable_at,
        splits=() if bar is None else splits,
        listing_units_per_share=listing_units_per_share,
    )
    if isinstance(basis, Unavailable):
        return None
    row = (
        shares
        if (basis.value, basis.knowable_at) == (shares.value, shares.knowable_at)
        else dataclasses.replace(shares, value=basis.value, knowable_at=basis.knowable_at)
    )
    return row, basis.factor != 1


def _checked(row: HistoryRow) -> HistoryRow:
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


_INSERT_SQL = """
insert into staging.strategy_backtest_inputs
    (issuer_id, cutoff_at, input_key, value, confidence, knowable_at, fiscal_period, recorded_at)
values (%s, %s, %s, %s, %s, %s, %s, clock_timestamp())
"""


def _write_new_rows(connection: psycopg.Connection[Any], issuer: HistoryIssuer, rows: Sequence[HistoryRow]) -> int:
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
    listing_ratio_reader: ListingRatioReader | None = None,
) -> HistorySummary:
    cutoffs = history_cutoffs(tick, months, schedule if schedule is not None else load_strategy_definition().schedule)
    extractor = headcount_extractor if headcount_extractor is not None else PostgresHeadcountExtractor(connection)
    reader = last_close if last_close is not None else postgres_last_close(connection)
    splits = (
        split_events
        if split_events is not None
        else postgres_split_events(connection, [issuer.ticker for issuer in issuers])
    )
    rules = ruleset if ruleset is not None else resolve_ruleset(connection)
    ratio_reader = listing_ratio_reader if listing_ratio_reader is not None else CoverRatioReader()
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
            listing_ratio_reader=ratio_reader,
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
