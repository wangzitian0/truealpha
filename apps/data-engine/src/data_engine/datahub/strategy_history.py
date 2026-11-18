from __future__ import annotations

import calendar
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, NamedTuple

import psycopg
from factors.backtest.corporate_actions import SplitEvent
from factors.snapshot_inputs import OperatingBranch
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.strategy import QuarterlyCutoffSchedule

from data_engine import raw_store
from data_engine.contracts.concept_mapping import ConceptMappingRuleset
from data_engine.contracts.datahub import CaptureWorkItem
from data_engine.datahub._helpers import require_registered_input_key, safe_case_id_part, single_origin_confidence
from data_engine.datahub.canonical_issuer import (
    ConflictingPredecessorError as ConflictingPredecessorError,
)
from data_engine.datahub.canonical_issuer import stored_entity_of
from data_engine.datahub.capture_core.concept_mapping import resolve_ruleset
from data_engine.datahub.capture_core.executor import FetchSuccess
from data_engine.datahub.capture_core.headcount import PostgresHeadcountExtractor
from data_engine.datahub.capture_core.issuer_registry import UNAVAILABLE_PAYLOAD_KEY
from data_engine.datahub.capture_core.parser_identity import PARSER_VERSION
from data_engine.datahub.capture_core.sec_financial_adapter import (
    HeadcountExtractor,
    ListingRatioReader,
    SecFinancialFactAdapter,
    SecTarget,
    build_bundle,
    company_facts_record_id,
    predecessor_ciks,
)
from data_engine.datahub.capture_core.source_registrations import last_settled_accession_date
from data_engine.datahub.input_cells import InputCell, financial_input_rows, vintage_knowable_at
from data_engine.datahub.price_series import load_daily_series, split_events_by_symbol
from data_engine.datahub.primary_listing import choose_primary_listings
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
    """Input is knowable on or after the cutoff calendar date."""


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


def _require_tz(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must carry a time zone")
    return value.astimezone(UTC)


def monthly_cutoffs(tick: datetime, months: int = DEFAULT_MONTHS) -> list[datetime]:
    utc_tick = _require_tz(tick, "tick")
    if months < 1:
        raise ValueError(f"months must be at least 1, got {months}")
    newest = utc_tick.year * 12 + utc_tick.month - 1
    return [datetime(idx // 12, idx % 12 + 1, 1, tzinfo=UTC) for idx in range(newest - months + 1, newest + 1)]


def history_cutoffs(tick: datetime, months: int, schedule: QuarterlyCutoffSchedule) -> list[datetime]:
    monthly = monthly_cutoffs(tick, months)
    return sorted(set(monthly) | set(quarter_end_cutoffs(schedule, start=monthly[0], end=tick)))


_QUARTER_END_MONTHS = (3, 6, 9, 12)


def quarter_end_cutoffs(schedule: QuarterlyCutoffSchedule, *, start: datetime, end: datetime) -> list[datetime]:
    first, last = _require_tz(start, "start"), _require_tz(end, "end")
    clock = schedule.first_cutoff_at.astimezone(UTC).timetz()
    return [
        cutoff
        for year in range(first.year, last.year + 1)
        for month in _QUARTER_END_MONTHS
        if first <= (cutoff := datetime.combine(date(year, month, calendar.monthrange(year, month)[1]), clock)) <= last
    ]


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


_LAST_CLOSE_SQL = (
    "select close, transaction_time from staging.market_prices_daily "
    "where symbol = %s and adjust = %s and close is not null and trading_date < %s and transaction_time < %s "
    "order by trading_date desc, recorded_at desc limit 1"
)


def _twelve_data_bar_confidence(symbol: str) -> Decimal:
    safe_symbol = safe_case_id_part(symbol, "symbol")
    return single_origin_confidence(
        case_id=f"history-close:{safe_symbol}",
        provider_id="origin:twelve-data:v1",
        lineage="twelve-data",
        evidence_id=f"history-close:{safe_symbol}",
    )


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
        "select raw_fetch_id from raw.capture_source_vintages where source_record_id = %s order by created_at desc, source_vintage_id desc limit 1",
        (company_facts_record_id(cik),),
    ).fetchone()
    if row is None:
        return None
    body = raw_store.get_payload(connection, row[0], store=store)
    return body, json.loads(body)


_LATEST_FINANCIAL_SQL = (
    "select entity_id, normalized_payload, source_record_id from ("
    "select distinct on (named.entity_id, p.normalized_payload ->> 'listing_id') "
    "named.entity_id, p.normalized_payload, v.source_record_id, "
    "p.normalized_payload ->> 'listing_id' as listing_id, o.recorded_at, o.knowable_at "
    "from unnest(%s::text[], %s::text[]) as named (issuer_id, entity_id) "
    "join staging.capture_observation_payloads p on p.normalized_payload ->> 'issuer_id' = named.issuer_id "
    "join staging.capture_normalized_observations o on o.observation_id = p.observation_id "
    "join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id "
    "where o.semantic_type = 'financial-fact' and o.parser_version = %s and p.normalized_payload ->> %s is distinct from 'true' "
    "order by named.entity_id, p.normalized_payload ->> 'listing_id', o.recorded_at desc, o.knowable_at desc, o.observation_id desc"
    ") newest order by entity_id, recorded_at desc, knowable_at desc, listing_id"
)


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
        ([p[0] for p in pairs], [p[1] for p in pairs], parser_version, UNAVAILABLE_PAYLOAD_KEY),
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
    ticker, branch = _ticker_of(listing_id, connection, known_at), payload.get("operating_branch")
    if ticker is None or branch is None:
        return None
    return HistoryIssuer(
        str(payload["issuer_id"]),
        str(payload.get("instrument_id", "")),
        listing_id,
        ticker,
        int(digits),
        OperatingBranch(branch),
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
    as_of = _require_tz(known_at, "known_at").date()
    ids_by_entity: dict[str, list[str]] = {}
    unresolved: list[str] = []
    for issuer_id in sorted(set(issuer_ids)):
        if (named := stored_entity_of(connection, issuer_id, as_of=as_of, known_at=known_at)) is None:
            unresolved.append(issuer_id)
        else:
            ids_by_entity.setdefault(str(named), []).append(issuer_id)
    observations = latest_financial_observations(
        connection,
        {e: sorted({e, *ids}) for e, ids in ids_by_entity.items()},
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
            issuers.append(replace(issuer, issuer_id=entity))
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
    found = predecessor_ciks(
        connection,
        [issuer.listing_id for issuer in issuers],
        {issuer.listing_id: issuer.issuer_id for issuer in issuers},
        candidate_ids_by_listing={
            i.listing_id: sorted({i.issuer_id, *ids_by_entity.get(i.issuer_id, ())}) for i in issuers
        },
    )
    return [
        issuer
        if (pred := found.get(issuer.listing_id)) is None or int(pred) == issuer.cik
        else replace(issuer, predecessor_cik=int(pred), predecessor_signed=pred.signed)
        for issuer in issuers
    ]


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
        if (stored := documents[cik]) is None:
            return None
        return build_bundle(stored[1], as_of, branch, raw_bytes=stored[0], ruleset=ruleset)

    rows: list[HistoryRow] = []
    no_outcome = undated = split_adjusted = 0
    splits: tuple[SplitEvent, ...] | None = None
    cover_concepts, statement_concepts = share_count_concepts(ruleset)
    bases = {COVER: 0, STATEMENT: 0, UNRECOGNISED: 0}
    for cutoff_at in cutoffs:
        as_of = last_settled_accession_date(cutoff_at)
        digest = canonical_sha256(
            {"issuer": issuer.issuer_id, "cutoff": cutoff_at.isoformat(), "kind": "strategy-history"}
        )
        item = CaptureWorkItem(
            campaign_id=f"capture-campaign:{digest}",
            source_request_id=f"source-request:{digest}",
            schedule_policy_id=f"schedule-policy:{digest}",
        )
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
        outcome = SecFinancialFactAdapter(
            {item.work_item_id: target},
            fetcher,
            headcount_extractor=headcount_extractor,
            ruleset=ruleset,
            listing_ratio_reader=ratio_reader,
        ).fetch(item)
        projected: list[InputCell] = []
        if isinstance(outcome, FetchSuccess):
            if outcome.record is None:
                raise RuntimeError(f"the SEC adapter returned a success without a record for {issuer.issuer_id}")
            payload = outcome.record.payload
            projected = financial_input_rows(
                payload, None, outcome.transaction_time, knowable_at_of=vintage_knowable_at(payload)
            )
            undated += len(financial_input_rows(payload, None, outcome.transaction_time)) - len(projected)
        else:
            no_outcome += 1
        bar = last_close(issuer.ticker, cutoff_at)
        for input_key, value, _unit, confidence, knowable_at, fiscal_period in projected:
            row = HistoryRow(issuer.issuer_id, cutoff_at, input_key, value, confidence, knowable_at, fiscal_period)
            if input_key == SHARES_KEY and fiscal_period is None:
                filed_date = row.knowable_at.astimezone(UTC).date()
                close_splits: tuple[SplitEvent, ...] = ()
                if bar is None:
                    basis_as_of, close_session, close_known = filed_date, filed_date, row.knowable_at
                else:
                    if splits is None:
                        splits = split_events(issuer.ticker)
                    basis_info = share_count_as_of(
                        (payload.get("vintage") or {}).get(SHARES_KEY),
                        row.value,
                        [stored[1] for stored in documents.values() if stored is not None],
                        cover_concepts=cover_concepts,
                        statement_concepts=statement_concepts,
                        filed_fallback=filed_date,
                    )
                    bases[basis_info.kind] += 1
                    basis_as_of = basis_info.as_of
                    close_session, close_known, close_splits = (
                        bar.knowable_at.astimezone(UTC).date(),
                        bar.knowable_at,
                        splits,
                    )
                basis = on_close_basis(
                    row.value,
                    row.knowable_at,
                    basis_date=basis_as_of,
                    close_session=close_session,
                    close_knowable_at=close_known,
                    splits=close_splits,
                    listing_units_per_share=listing_units_per_share_of(payload),
                )
                if isinstance(basis, Unavailable):
                    continue
                if (basis.value, basis.knowable_at) != (row.value, row.knowable_at):
                    row = replace(row, value=basis.value, knowable_at=basis.knowable_at)
                split_adjusted += basis.factor != 1
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
        tuple(rows), no_outcome, undated, split_adjusted, bases[COVER], bases[STATEMENT], bases[UNRECOGNISED]
    )


def _checked(row: HistoryRow) -> HistoryRow:
    require_registered_input_key(row.input_key, verb="projecting")
    admit_input(row.cutoff_at, row.knowable_at)
    return row


_EXISTING_SQL = "select cutoff_at, input_key, coalesce(fiscal_period, ''), value, knowable_at from staging.strategy_backtest_inputs where issuer_id = %s and cutoff_at = any(%s)"
_INSERT_SQL = "insert into staging.strategy_backtest_inputs (issuer_id, cutoff_at, input_key, value, confidence, knowable_at, fiscal_period, recorded_at) values (%s, %s, %s, %s, %s, %s, %s, clock_timestamp())"


def _write_new_rows(connection: psycopg.Connection[Any], issuer: HistoryIssuer, rows: Sequence[HistoryRow]) -> int:
    present = {
        (cut, key, period, Decimal(str(val)), know_at)
        for cut, key, period, val, know_at in connection.execute(
            _EXISTING_SQL, (issuer.issuer_id, sorted({r.cutoff_at for r in rows}))
        ).fetchall()
    }
    fresh = [
        r for r in rows if (r.cutoff_at, r.input_key, r.fiscal_period or "", r.value, r.knowable_at) not in present
    ]
    with connection.cursor() as cursor:
        cursor.executemany(
            _INSERT_SQL,
            [
                (r.issuer_id, r.cutoff_at, r.input_key, r.value, r.confidence, r.knowable_at, r.fiscal_period)
                for r in fresh
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
    cutoffs = history_cutoffs(tick, months, schedule or load_strategy_definition().schedule)
    extractor = headcount_extractor or PostgresHeadcountExtractor(connection)
    reader = last_close or postgres_last_close(connection)
    splits = split_events or postgres_split_events(connection, [issuer.ticker for issuer in issuers])
    rules = ruleset or resolve_ruleset(connection)
    ratio_reader = listing_ratio_reader or CoverRatioReader()
    inserted = already_present = no_outcome = undated = split_adj = cover = stmt = unrec = 0
    for issuer in issuers:
        proj = project_issuer_rows(
            issuer,
            cutoffs,
            load_facts=load_facts,
            headcount_extractor=extractor,
            last_close=reader,
            split_events=splits,
            ruleset=rules,
            listing_ratio_reader=ratio_reader,
        )
        skipped = _write_new_rows(connection, issuer, proj.rows)
        already_present += skipped
        inserted += len(proj.rows) - skipped
        no_outcome += proj.no_outcome
        undated += proj.undated
        split_adj += proj.split_adjusted
        cover += proj.share_basis_cover
        stmt += proj.share_basis_statement
        unrec += proj.share_basis_unrecognised
    return HistorySummary(
        len(issuers), len(cutoffs), inserted, already_present, no_outcome, undated, (), 0, split_adj, cover, stmt, unrec
    )


def project_deployed_history(
    connection: psycopg.Connection[Any], *, tick: datetime, months: int = DEFAULT_MONTHS, store: Any | None = None
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
    return replace(summary, unresolved_issuers=canonical.unresolved, merged_ids=len(canonical.merged))
