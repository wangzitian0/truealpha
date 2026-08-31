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


def figi_from_raw(connection, isins: list[str], *, store=None) -> tuple[dict[str, list[dict]], list[str]]:
    """Rebuild the ISIN->records mapping from OpenFIGI batches already landed in
    raw — newest batch wins per ISIN. Returns (mapping, still-missing ISINs).
    Ticker-keyed OpenFIGI batches (the universe plane's) carry no `isins`
    metadata and are skipped rather than misread."""
    rows = connection.execute(
        "select id, metadata from raw.fetches where source = %s order by id",
        (DataSource.OPENFIGI.value,),
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for fetch_id, metadata in rows:
        batch_isins = (metadata or {}).get("isins", [])
        if not batch_isins:
            continue
        results = json.loads(raw_store.get_payload(connection, fetch_id, store=store))
        for isin, job in zip(batch_isins, results):
            out[isin] = job.get("data", [])
    return {i: out[i] for i in isins if i in out}, [i for i in isins if i not in out]


def enrich_holding_identities(
    connection,
    *,
    api_key: str = "",
    http=None,
    ticker_cik: dict[str, int] | None = None,
    now: datetime | None = None,
    store=None,
) -> EnrichmentOutcome:
    """One idempotent sweep over every still-minted holding identity.

    `ticker_cik` and `http` are seams: deployed callers pass neither (the SEC
    index and client are built lazily, only when something needs resolving).
    """
    at = now or datetime.now(UTC)
    rows = connection.execute(
        "select distinct isin from staging.fund_holding_facts where isin is not null order by isin"
    ).fetchall()
    minted = [
        isin for (isin,) in rows if (er.resolve(connection, "isin", isin, as_of=at) or "").startswith(_MINTED_PREFIX)
    ]
    if not minted:
        return EnrichmentOutcome(0, 0, 0, 0, 0)

    mapping, missing = figi_from_raw(connection, minted, store=store)
    raw_ref_by_isin: dict[str, str] = {}
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
                raw_ref_by_isin[isin] = raw_store.raw_ref(fetch_id)

        mapping.update(openfigi.map_isins(client, missing, api_key=api_key, on_batch=persist_batch))
        fetched = len(missing)

    # The identifier vintage's transaction time is when the mapping became
    # knowable: this sweep's clock for live fetches, the newest landed batch's
    # clock for raw reuse — never an unrelated now().
    reused_at = connection.execute(
        "select max(fetched_at) from raw.fetches where source = %s and metadata ? 'isins'",
        (DataSource.OPENFIGI.value,),
    ).fetchone()[0]
    newest_raw = connection.execute(
        "select max(id) from raw.fetches where source = %s and metadata ? 'isins'",
        (DataSource.OPENFIGI.value,),
    ).fetchone()[0]
    fallback_ref = raw_store.raw_ref(newest_raw) if newest_raw is not None else "raw.fetches:0"

    index: dict[str, int] | None = ticker_cik
    repointed = unmapped = 0
    for isin in minted:
        listing = pick_listing(mapping.get(isin, []), isin)
        ticker = sec_ticker(listing) if listing else None
        if ticker is not None and index is None:
            index = sec.ticker_cik_index(http) if http is not None else sec.ticker_cik_index()
        cik = index.get(ticker) if ticker is not None and index is not None else None
        if cik is None:
            unmapped += 1
            continue
        target = f"issuer:cik:{cik:010d}"
        minted_id = f"{_MINTED_PREFIX}{isin}"
        transaction_time = at if isin in raw_ref_by_isin else (reused_at or at)
        raw_ref = raw_ref_by_isin.get(isin, fallback_ref)
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
        # Forward resolution for the immutable history: fund_holding_facts rows
        # keep the minted holding_id; the merge hop makes them join the plane.
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
