"""The missing-cell planner (#733): which (standard, issuer) cells are open at a cutoff.

Start from the declared demand (every issuer in the universe for every standard) and
left-join what exists. A cell is open when the issuer has no fact knowable at the cutoff,
when its best fact does not carry the standard's evidence, or when its best fact is older
than the standard's cadence or superseded by a newer annual filing (#1519).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from truealpha_contracts.standards import MetricStandard

from data_engine.datahub.canonical_issuer import signed_predecessor_ciks
from data_engine.datahub.primary_listing import choose_primary_listings
from data_engine.datahub.production_topt.headcount import _is_eligible_headcount_row
from data_engine.datahub.production_topt.universe_corpus import TOPT_CORPUS_FILENAME, load_corpus
from data_engine.datahub.production_topt.universe_plane import resolve_universe_corpus
from data_engine.datahub.resolve_coordinates import alias_of, is_uuid, lookup_entity

TOPT_UNIVERSE = "topt"
_HEADCOUNT_TABLE = "staging.issuer_headcount_facts"
_NEWER_ANNUAL_FILING_SQL = (
    "select 1 from raw.fetches where source = 'sec' and source_record_id like %s "
    "and source_published_at > %s and source_published_at <= %s "
    "and coalesce(metadata->>'form', '10-K') in ('10-K', '20-F') "
    "union all select 1 from staging.capture_normalized_observations o "
    "join staging.capture_observation_payloads p on p.observation_id = o.observation_id "
    "join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id "
    "where o.semantic_type = 'financial-fact' and v.source_record_id = %s "
    "and o.knowable_at > %s and o.knowable_at <= %s "
    "and p.normalized_payload->'vintage'->'revenue'->>'form' in ('10-K', '20-F') limit 1"
)

OpenReason = Literal["no_fact", "seed_only", "stale"]


@dataclass(frozen=True)
class UniverseIssuer:
    issuer_id: str
    ticker: str
    listing_id: str
    cik: int | None
    predecessor_cik: int | None = None


@dataclass(frozen=True)
class OpenCell:
    issuer: UniverseIssuer
    reason: OpenReason
    best_source: str | None
    best_knowable_at: datetime | None


def universe_issuers(connection: Any, universe: str, *, cutoff: datetime | None = None) -> list[UniverseIssuer]:
    """One entry per issuer, CIK-resolved at `cutoff` where the universe plane resolved it."""
    known_at = cutoff.astimezone(UTC) if cutoff is not None else datetime.now(UTC)
    as_of = known_at.date()
    corpus = (
        load_corpus(TOPT_CORPUS_FILENAME)
        if universe == TOPT_UNIVERSE
        else resolve_universe_corpus(connection, universe)
    )
    rows_by_issuer: dict[str, list[list[str]]] = {}
    for row in corpus["topt_denominator"]["instruments"]:
        rows_by_issuer.setdefault(row[0], []).append(row)
    primary = choose_primary_listings(
        connection,
        {issuer_id: [row[2] for row in rows] for issuer_id, rows in rows_by_issuer.items()},
        known_at=known_at,
    )
    issuers: dict[str, UniverseIssuer] = {}
    for issuer_id, rows in rows_by_issuer.items():
        _issuer, _security_id, listing_id, ticker = next((row for row in rows if row[2] == primary[issuer_id]), rows[0])
        cik: int | None = None
        if issuer_id.startswith("issuer:cik:"):
            cik = int(issuer_id.removeprefix("issuer:cik:"))
        elif is_uuid(issuer_id):
            _cik_val = alias_of(connection, issuer_id, "cik", valid_at=as_of, known_at=known_at)
            try:
                cik = int(_cik_val) if _cik_val is not None else None
            except (ValueError, TypeError):
                cik = None
        else:
            try:
                entity_uuid = lookup_entity(connection, issuer_id, "issuer", as_of=as_of, known_at=known_at)
                if entity_uuid is not None:
                    _cik_val = alias_of(connection, entity_uuid, "cik", valid_at=as_of, known_at=known_at)
                    if _cik_val is not None:
                        cik = int(_cik_val)
            except Exception:  # noqa: BLE001
                cik = None
        issuers[issuer_id] = UniverseIssuer(issuer_id=issuer_id, ticker=ticker, listing_id=listing_id, cik=cik)
    predecessors = signed_predecessor_ciks(connection, {issuer_id: issuer_id for issuer_id in issuers})
    return [
        replace(issuer, predecessor_cik=int(predecessors[issuer.issuer_id]))
        if issuer.issuer_id in predecessors
        else issuer
        for issuer in issuers.values()
    ]


def resolve_missing_ciks(issuers: list[UniverseIssuer], ticker_index: dict[str, int]) -> list[UniverseIssuer]:
    """Resolve LEI-keyed issuers through SEC's ticker crosswalk (BRK.B -> BRK-B)."""
    return [
        issuer if issuer.cik is not None else replace(issuer, cik=ticker_index.get(issuer.ticker.replace(".", "-")))
        for issuer in issuers
    ]


def _resolve_headcount_row(connection: Any, cik: int, cutoff: datetime, priority: list[str], first_row: Any) -> Any:
    """Apply the same citation eligibility contract as `lookup_headcount` (#1519)."""
    if len(first_row) >= 4 and not _is_eligible_headcount_row(
        knowable_at=first_row[1], source=str(first_row[0]), evidence_ref=str(first_row[2]), period_end=first_row[3]
    ):
        return None
    try:
        rows = connection.execute(
            f"select source, knowable_at, evidence_ref, period_end from {_HEADCOUNT_TABLE} "
            "where cik = %s and knowable_at <= %s and source <> 'curated-10k-2025' "
            "order by array_position(%s::text[], source) nulls last, knowable_at desc, id desc",
            (cik, cutoff, priority),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return first_row
    if not rows:
        return first_row
    return next(
        (
            r
            for r in rows
            if len(r) < 4
            or _is_eligible_headcount_row(knowable_at=r[1], source=str(r[0]), evidence_ref=str(r[2]), period_end=r[3])
        ),
        None,
    )


def _has_newer_annual_filing(connection: Any, cik: int, fact_knowable_at: datetime, cutoff: datetime) -> bool:
    """Return True when a newer 10-K/20-F is knowable at `cutoff` after `fact_knowable_at` (#1519)."""
    try:
        row = connection.execute(
            _NEWER_ANNUAL_FILING_SQL,
            (
                f"filing-document:CIK{cik:010d}:%",
                fact_knowable_at,
                cutoff,
                f"companyfacts:CIK{cik:010d}",
                fact_knowable_at,
                cutoff,
            ),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return False
    return row is not None


def open_cells(
    connection: Any, issuers: list[UniverseIssuer], *, standard: MetricStandard, cutoff: datetime
) -> list[OpenCell]:
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be timezone-aware")
    stale_before = cutoff - timedelta(days=standard.max_age_days)
    plane = standard.plane
    priority = list(plane.reconciliation_policy.source_priority)
    withdrawn = list(plane.withdrawn_extractors)
    headcount_filter = "and source <> 'curated-10k-2025'" if plane.table == _HEADCOUNT_TABLE else ""
    cells: list[OpenCell] = []
    for issuer in issuers:
        if issuer.cik is None:
            cells.append(OpenCell(issuer, "no_fact", None, None))
            continue
        row = connection.execute(
            f"""
            select source, knowable_at
            from {plane.table}
            where {plane.issuer_column} = %s and knowable_at <= %s
            {headcount_filter}
            {"and extractor <> all(%s::text[])" if withdrawn else ""}
            order by array_position(%s::text[], source) nulls last, knowable_at desc, id desc
            limit 1
            """,  # noqa: S608 - identifiers come from the standard's declared plane, not input
            (issuer.cik, cutoff, *([withdrawn] if withdrawn else []), priority),
        ).fetchone()
        if row is not None and plane.table == _HEADCOUNT_TABLE:
            row = _resolve_headcount_row(connection, issuer.cik, cutoff, priority, row)
        if row is None:
            cells.append(OpenCell(issuer, "no_fact", None, None))
        elif row[0] not in standard.evidence_bearing_sources:
            cells.append(OpenCell(issuer, "seed_only", row[0], row[1]))
        elif row[1] < stale_before or _has_newer_annual_filing(connection, issuer.cik, row[1], cutoff):
            cells.append(OpenCell(issuer, "stale", row[0], row[1]))
    return cells
