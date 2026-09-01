"""#63 second tranche: re-point minted holding identities onto the plane's keying.

Tranche 1 (nport_holdings) mints `company:isin:<isin>` for every ISIN the KG has
never seen — durable, honest, and joinable to nothing. This module closes the
loop the way bootstrap_universe always has: OpenFIGI maps the ISIN to its US
listing, the SEC ticker file maps the listing to a CIK, and a NEWER identifier
vintage re-points `resolve("isin", ...)` at the plane-keyed issuer entity. No
row is ever rewritten; the historical fund_holding_facts rows keep their minted
`holding_id` and resolve forward through the one `same_as` edge written here —
the only reason this codebase ever writes one (append-only facts cannot be
re-keyed in place).

Identity shape: the target is `issuer:cik:{cik:010d}` — the id the deployed
plane (universe heads, mart.topt_core_results) actually keys by — NOT
bootstrap's bare `company:cik:{cik}`. The two shapes coexist historically;
new enrichment aligns with what production joins against (#63 issue note).

Offline-first: OpenFIGI batches land in raw with `metadata={"isins": batch}`,
so every later run (and every KG rebuild) resolves from the landing zone
without re-spending the rate limit. Only never-seen ISINs reach the vendor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from factors.shared import entity_resolution as er
from truealpha_contracts.models import DataSource

from data_engine import raw_store
from data_engine.sources import openfigi, sec
from data_engine.universe import pick_listing, sec_ticker

# OpenFIGI's mapping is authoritative but not a filing; same confidence
# bootstrap_universe assigns its openfigi-sourced identifiers.
CONF_OPENFIGI = 0.98

_MINTED_PREFIX = "company:isin:"


@dataclass(frozen=True)
class EnrichmentOutcome:
    """One enrichment sweep, sized for the op log."""

    unresolved: int  # minted identities considered this sweep
    reused_from_raw: int  # ISINs answered from landed OpenFIGI batches
    fetched: int  # ISINs sent to the vendor this sweep
    repointed: int  # identities re-keyed onto issuer:cik entities
    unmapped: int  # no US listing / no CIK — stay minted, retried next sweep

    def __str__(self) -> str:
        return (
            f"holdings-enrichment: unresolved={self.unresolved} "
            f"reused={self.reused_from_raw} fetched={self.fetched} "
            f"repointed={self.repointed} unmapped={self.unmapped}"
        )


@dataclass(frozen=True)
class _RawMapping:
    """One ISIN's records plus the batch that supplied them — the batch's own
    clock and pointer, so nothing downstream borrows a newer batch's time."""

    records: list[dict]
    raw_ref: str
    fetched_at: datetime


def _scan_isin_batches(connection, isins: list[str], *, store=None) -> dict[str, _RawMapping]:
    """Newest-batch-wins ISIN->records from the landing zone. Filtered in SQL:
    only ISIN-keyed batches (the universe plane's ticker-keyed ones carry no
    `isins` metadata) and only batches intersecting the requested set are read
    and decoded (review on #695 — the scan must not grow with unrelated raw)."""
    if not isins:
        return {}
    rows = connection.execute(
        """
        select id, fetched_at, metadata from raw.fetches
        where source = %s and metadata ? 'isins' and metadata->'isins' ?| %s::text[]
        order by id
        """,
        (DataSource.OPENFIGI.value, isins),
    ).fetchall()
    wanted = set(isins)
    out: dict[str, _RawMapping] = {}
    for fetch_id, fetched_at, metadata in rows:
        batch_isins = metadata.get("isins", [])
        results = json.loads(raw_store.get_payload(connection, fetch_id, store=store))
        for isin, job in zip(batch_isins, results):
            if isin in wanted:
                out[isin] = _RawMapping(
                    records=job.get("data", []),
                    raw_ref=raw_store.raw_ref(fetch_id),
                    fetched_at=fetched_at,
                )
    return out


def figi_from_raw(connection, isins: list[str], *, store=None) -> tuple[dict[str, list[dict]], list[str]]:
    """Rebuild the ISIN->records mapping from OpenFIGI batches already landed in
    raw — newest batch wins per ISIN. Returns (mapping, still-missing ISINs)."""
    found = _scan_isin_batches(connection, isins, store=store)
    return {i: found[i].records for i in isins if i in found}, [i for i in isins if i not in found]


def enrich_holding_identities(
    connection,
    *,
    api_key: str = "",
    http=None,
    ticker_cik: dict[str, int] | None = None,
    now: datetime | None = None,
    store=None,
    isins: list[str] | None = None,
) -> EnrichmentOutcome:
    """One idempotent sweep over every still-minted holding identity.

    `ticker_cik`, `http` and `isins` are seams: deployed callers pass none of
    them (the SEC index and client are built lazily, and the sweep covers every
    holding ISIN); tests scope `isins` to their own seeds so a shared database
    cannot leak foreign rows into their counters.
    """
    at = now or datetime.now(UTC)
    if isins is None:
        rows = connection.execute(
            "select distinct isin from staging.fund_holding_facts where isin is not null order by isin"
        ).fetchall()
    else:
        rows = [(isin,) for isin in sorted(isins)]
    # Two reasons an ISIN needs the sweep: its identity still resolves to the
    # mint (repoint work), or its per-ISIN ticker association is missing — the
    # share-class crosswalk (#706): the issuer's newest ticker vintage collapses
    # GOOG onto GOOGL, so each ISIN keeps ITS OWN ticker on the minted per-ISIN
    # entity, which the valuation view reads first. Already-landed OpenFIGI
    # batches make the backfill offline.
    minted = []
    for (isin,) in rows:
        needs_repoint = (er.resolve(connection, "isin", isin, as_of=at) or "").startswith(_MINTED_PREFIX)
        has_own_ticker = (
            connection.execute(
                "select 1 from staging.kg_identifiers where entity_id = %s and identifier_type = 'ticker' limit 1",
                (f"{_MINTED_PREFIX}{isin}",),
            ).fetchone()
            is not None
        )
        if needs_repoint or not has_own_ticker:
            minted.append(isin)
    if not minted:
        return EnrichmentOutcome(0, 0, 0, 0, 0)

    # Each ISIN keeps ITS OWN batch's clock and pointer — the vintage's
    # transaction time is when THAT mapping became knowable, never a newer
    # batch's (review on #695).
    found = _scan_isin_batches(connection, minted, store=store)
    mapping = {isin: entry.records for isin, entry in found.items()}
    provenance = {isin: (entry.raw_ref, entry.fetched_at) for isin, entry in found.items()}
    missing = [isin for isin in minted if isin not in found]
    fetched = 0
    if missing:
        client = http or sec.client()

        def persist_batch(batch: list[str], results) -> None:
            fetch_id = raw_store.insert_json_fetch(
                connection,
                source=DataSource.OPENFIGI,
                source_record_id=f"mapping:{batch[0]}",
                payload=results,
                fetched_at=at,
                metadata={"isins": batch},
                store=store,
            )
            for isin in batch:
                provenance[isin] = (raw_store.raw_ref(fetch_id), at)

        mapping.update(openfigi.map_isins(client, missing, api_key=api_key, on_batch=persist_batch))
        fetched = len(missing)

    index: dict[str, int] | None = ticker_cik
    repointed = unmapped = 0
    for isin in minted:
        listing = pick_listing(mapping.get(isin, []), isin)
        ticker = sec_ticker(listing) if listing is not None else None
        if listing is None or ticker is None:
            unmapped += 1
            continue
        if index is None:
            index = sec.ticker_cik_index(http) if http is not None else sec.ticker_cik_index()
        cik = index.get(ticker)
        if cik is None:
            unmapped += 1
            continue
        target = f"issuer:cik:{cik:010d}"
        minted_id = f"{_MINTED_PREFIX}{isin}"
        raw_ref, transaction_time = provenance[isin]
        # The per-ISIN ticker rides the minted entity — THIS ISIN's own listing
        # (#706): the issuer-level ticker below stays newest-wins and collapses
        # share classes by design; readers that must not collapse resolve here.
        # Its OWN source name, because kg_identifiers is unique on
        # (source, type, value, transaction_time): under source="openfigi" this
        # row and the issuer-level row are the same tuple whenever both derive
        # from the same landed batch, and whichever asserts second is silently
        # swallowed — on prod that was this one, 0 rows landed while the sweep
        # reported success. A listing-association claim is a different claim;
        # it gets a different source.
        er.ensure_entity(connection, minted_id, "company", listing.name or isin)
        er.assert_identifier(
            connection,
            entity_id=minted_id,
            source="openfigi-listing",
            identifier_type="ticker",
            identifier_value=ticker,
            confidence=CONF_OPENFIGI,
            transaction_time=transaction_time,
            valid_from=transaction_time.date().isoformat(),
            raw_ref=raw_ref,
        )
        if (er.resolve(connection, "isin", isin, as_of=at) or "").startswith(_MINTED_PREFIX):
            er.ensure_entity(connection, target, "company", listing.name or ticker)
            for id_type, id_value in (("isin", isin), ("ticker", ticker)):
                er.assert_identifier(
                    connection,
                    entity_id=target,
                    source="openfigi",
                    identifier_type=id_type,
                    identifier_value=id_value,
                    confidence=CONF_OPENFIGI,
                    transaction_time=transaction_time,
                    valid_from=transaction_time.date().isoformat(),
                    raw_ref=raw_ref,
                )
            # Forward resolution for the immutable history: fund_holding_facts
            # rows keep the minted holding_id; the merge hop joins the plane.
            er.add_edge(
                connection,
                from_id=minted_id,
                to_id=target,
                relation_type="same_as",
                confidence=CONF_OPENFIGI,
                source="openfigi",
                transaction_time=transaction_time,
                valid_from=transaction_time.date().isoformat(),
                raw_ref=raw_ref,
            )
            repointed += 1

    return EnrichmentOutcome(
        unresolved=len(minted),
        reused_from_raw=len(minted) - len(missing),
        fetched=fetched,
        repointed=repointed,
        unmapped=unmapped,
    )
