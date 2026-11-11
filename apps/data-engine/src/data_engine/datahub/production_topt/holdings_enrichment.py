"""Enrich minted N-PORT holding identities onto plane-keyed issuer entities."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from factors.shared import entity_resolution as er
from truealpha_contracts.models import DataSource

from data_engine import raw_store
from data_engine.sources import openfigi, sec
from data_engine.universe import pick_listing, sec_ticker

CONF_OPENFIGI = 0.98
_MINTED_PREFIX = "company:isin:"


@dataclass(frozen=True)
class EnrichmentOutcome:
    unresolved: int
    reused_from_raw: int
    fetched: int
    repointed: int
    unmapped: int

    def __str__(self) -> str:
        return (
            f"holdings-enrichment: unresolved={self.unresolved} "
            f"reused={self.reused_from_raw} fetched={self.fetched} "
            f"repointed={self.repointed} unmapped={self.unmapped}"
        )


@dataclass(frozen=True)
class _RawMapping:
    records: list[dict]
    raw_ref: str
    fetched_at: datetime


def _scan_isin_batches(connection, isins: list[str], *, store=None) -> dict[str, _RawMapping]:
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
    found = _scan_isin_batches(connection, isins, store=store)
    return {i: found[i].records for i in isins if i in found}, [i for i in isins if i not in found]


def _holding_display_name(connection, *, isin: str, ticker: str, listing_name: str | None) -> str | None:
    if name := (listing_name or "").strip():
        return name
    row = connection.execute(
        "select nullif(btrim(holding_name), '') from staging.fund_holding_facts"
        " where isin = %s and nullif(btrim(holding_name), '') is not null"
        " and upper(btrim(holding_name)) not in (upper(btrim(%s)), upper(btrim(%s)))"
        " order by transaction_time desc limit 1",
        (isin, ticker, isin),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


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
    at = now or datetime.now(UTC)
    rows = (
        connection.execute(
            "select distinct isin from staging.fund_holding_facts where isin is not null order by isin"
        ).fetchall()
        if isins is None
        else [(isin,) for isin in sorted(isins)]
    )
    minted = [
        isin
        for (isin,) in rows
        if (er.resolve(connection, "isin", isin, as_of=at) or "").startswith(_MINTED_PREFIX)
        or connection.execute(
            "select 1 from staging.kg_identifiers where entity_id = %s and identifier_type = 'ticker' limit 1",
            (f"{_MINTED_PREFIX}{isin}",),
        ).fetchone()
        is None
    ]
    if not minted:
        return EnrichmentOutcome(0, 0, 0, 0, 0)

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
        minted_id = f"{_MINTED_PREFIX}{isin}"
        raw_ref, transaction_time = provenance[isin]
        valid_from = transaction_time.date().isoformat()
        er.ensure_entity(connection, minted_id, "company", listing.name or isin)
        er.assert_identifier(
            connection,
            entity_id=minted_id,
            source="openfigi-listing",
            identifier_type="ticker",
            identifier_value=ticker,
            confidence=CONF_OPENFIGI,
            transaction_time=transaction_time,
            valid_from=valid_from,
            raw_ref=raw_ref,
        )
        if index is None:
            index = sec.ticker_cik_index(http) if http is not None else sec.ticker_cik_index()
        cik = index.get(ticker)
        if cik is None:
            unmapped += 1
            continue
        target = f"issuer:cik:{cik:010d}"
        if (er.resolve(connection, "isin", isin, as_of=at) or "").startswith(_MINTED_PREFIX):
            er.ensure_entity(
                connection,
                target,
                "company",
                _holding_display_name(connection, isin=isin, ticker=ticker, listing_name=listing.name) or target,
            )
            for id_type, id_value in (("isin", isin), ("ticker", ticker)):
                er.assert_identifier(
                    connection,
                    entity_id=target,
                    source="openfigi",
                    identifier_type=id_type,
                    identifier_value=id_value,
                    confidence=CONF_OPENFIGI,
                    transaction_time=transaction_time,
                    valid_from=valid_from,
                    raw_ref=raw_ref,
                )
            er.add_edge(
                connection,
                from_id=minted_id,
                to_id=target,
                relation_type="same_as",
                confidence=CONF_OPENFIGI,
                source="openfigi",
                transaction_time=transaction_time,
                valid_from=valid_from,
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
