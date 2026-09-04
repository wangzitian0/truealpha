"""The missing-cell planner (#733): which (standard, issuer) cells are open at a cutoff.

Rule 24's shape applied to backfill: start from the DECLARED demand — every issuer in the
universe for every standard — and left-join what exists. A cell is open when the issuer
has no fact knowable at the cutoff, when its best fact does not carry the standard's
evidence (a reviewed seed, #521), or when its best fact is older than the standard's
cadence. Open cells are the loop's work; closed cells are never re-fetched, so a run's
vendor spend is proportional to what is actually missing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Literal

from truealpha_contracts.standards import MetricStandard

from data_engine.datahub.production_topt.headcount import HEADCOUNT_SOURCE_PRIORITY
from data_engine.datahub.production_topt.universe_corpus import load_corpus
from data_engine.datahub.production_topt.universe_plane import resolve_universe_corpus

TOPT_UNIVERSE = "topt"
TOPT_CORPUS_FILENAME = "corpus.v1.json"

OpenReason = Literal["no_fact", "seed_only", "stale"]


@dataclass(frozen=True)
class UniverseIssuer:
    issuer_id: str
    ticker: str
    listing_id: str
    cik: int | None
    # #496: a post-reorganization holding company (XOM's 2115436) files nothing under its
    # new CIK for a while; the owner-signed registry names the CIK whose filings still
    # describe the issuer. Consulted only when the current CIK has no annual filing.
    predecessor_cik: int | None = None


@dataclass(frozen=True)
class OpenCell:
    issuer: UniverseIssuer
    reason: OpenReason
    best_source: str | None
    best_knowable_at: datetime | None


def universe_issuers(connection: Any, universe: str) -> list[UniverseIssuer]:
    """One entry per issuer (a dual-listed issuer appears once), CIK-resolved where the
    universe plane already resolved it. `topt` is the hand-curated packaged corpus; any
    other value is a governed universe head kind (`universe-list:qqq`)."""
    corpus = (
        load_corpus(TOPT_CORPUS_FILENAME)
        if universe == TOPT_UNIVERSE
        else resolve_universe_corpus(connection, universe)
    )
    issuers: dict[str, UniverseIssuer] = {}
    for issuer_id, _security_id, listing_id, ticker in corpus["topt_denominator"]["instruments"]:
        if issuer_id in issuers:
            continue
        cik = int(issuer_id.removeprefix("issuer:cik:")) if issuer_id.startswith("issuer:cik:") else None
        issuers[issuer_id] = UniverseIssuer(issuer_id=issuer_id, ticker=ticker, listing_id=listing_id, cik=cik)
    predecessors = dict(
        connection.execute(
            "select issuer_id, predecessor_cik from staging.issuer_cik_predecessors where issuer_id = any(%s)",
            (sorted(issuers),),
        ).fetchall()
    )
    return [
        replace(issuer, predecessor_cik=int(predecessors[issuer.issuer_id]))
        if issuer.issuer_id in predecessors
        else issuer
        for issuer in issuers.values()
    ]


def resolve_missing_ciks(issuers: list[UniverseIssuer], ticker_index: dict[str, int]) -> list[UniverseIssuer]:
    """LEI-keyed issuers (the TOPT corpus) resolve through SEC's ticker crosswalk, the same
    rule `build_routes` applies; the hyphen form is SEC's (BRK.B -> BRK-B)."""
    resolved: list[UniverseIssuer] = []
    for issuer in issuers:
        if issuer.cik is not None:
            resolved.append(issuer)
            continue
        cik = ticker_index.get(issuer.ticker.replace(".", "-"))
        resolved.append(replace(issuer, cik=cik))
    return resolved


def open_cells(
    connection: Any, issuers: list[UniverseIssuer], *, standard: MetricStandard, cutoff: datetime
) -> list[OpenCell]:
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be timezone-aware")
    stale_before = cutoff - timedelta(days=standard.max_age_days)
    cells: list[OpenCell] = []
    for issuer in issuers:
        if issuer.cik is None:
            cells.append(OpenCell(issuer, "no_fact", None, None))
            continue
        # The SAME resolution the daily tick's reader applies (rule 12: declared source
        # priority first, recency second): a cited extraction with an older filing date
        # outranks a later-stamped seed, so the cell is closed, not refetched (review on
        # #740).
        row = connection.execute(
            """
            select source, knowable_at
            from staging.issuer_headcount_facts
            where cik = %s and knowable_at <= %s
            order by array_position(%s::text[], source) nulls last, knowable_at desc, id desc
            limit 1
            """,
            (issuer.cik, cutoff, list(HEADCOUNT_SOURCE_PRIORITY)),
        ).fetchone()
        if row is None:
            cells.append(OpenCell(issuer, "no_fact", None, None))
        elif row[0] not in standard.evidence_bearing_sources:
            cells.append(OpenCell(issuer, "seed_only", row[0], row[1]))
        elif row[1] < stale_before:
            cells.append(OpenCell(issuer, "stale", row[0], row[1]))
    return cells
