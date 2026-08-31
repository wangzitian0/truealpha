"""#63 tranche 2: minted company:isin identities re-point onto issuer:cik
entities from landed OpenFIGI batches — offline, idempotent, forward-resolving.

The fixture seeds what tranche 1 leaves behind (facts + minted entities) plus a
landed ISIN-keyed OpenFIGI batch, then runs the sweep with NO http client: the
whole resolution must come from the landing zone and the injected SEC index."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from data_engine import raw_store
from data_engine.config import settings
from data_engine.datahub.production_topt.holdings_enrichment import (
    enrich_holding_identities,
    figi_from_raw,
)
from factors.shared import entity_resolution as er
from truealpha_contracts import RawCapture, RawIngestionEnvelope, RawObjectRef
from truealpha_contracts.models import DataSource


class _InMemoryObjectStore:
    """RawObjectStore over a dict — the raw landing path without MinIO (the
    degraded-capture suite's exact seam)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def store(self, capture: RawCapture) -> RawIngestionEnvelope:
        digest = hashlib.sha256(capture.body).hexdigest()
        key = f"raw/{capture.source.value}/{digest[:2]}/{digest}"
        self.objects[key] = capture.body
        return RawIngestionEnvelope(
            source=capture.source,
            source_record_id=capture.source_record_id,
            object=RawObjectRef(
                bucket="truealpha-raw",
                key=key,
                sha256=digest,
                byte_length=len(capture.body),
                content_type=capture.content_type,
            ),
            fetched_at=capture.fetched_at,
            source_published_at=capture.source_published_at,
            metadata=capture.metadata,
        )

    def get(self, ref: RawObjectRef) -> bytes:
        return self.objects[ref.key]


_AT = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
_NVDA_ISIN = "US67066G1040"
_FOREIGN_ISIN = "XS0000000001"  # maps to no US listing — must stay minted
_NVDA_RECORDS = [
    {
        "figi": "BBG000BLARC1",
        "ticker": "NVDA",
        "exchCode": "US",
        "name": "NVIDIA CORP",
        "securityType": "Common Stock",
        "marketSector": "Equity",
    }
]


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        pytest.skip(f"no local database: {error}")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def _seed_tranche_one(connection) -> None:
    er.ensure_entity(connection, "etf:series:S-TEST-ENRICH", "etf", "Enrichment Test Fund")
    for isin in (_NVDA_ISIN, _FOREIGN_ISIN):
        minted = f"company:isin:{isin}"
        er.ensure_entity(connection, minted, "company", isin)
        er.assert_identifier(
            connection,
            entity_id=minted,
            source="nport",
            identifier_type="isin",
            identifier_value=isin,
            confidence=1.0,
            transaction_time=_AT,
            valid_from="2026-06-30",
            raw_ref="raw.fetches:0",
        )
        connection.execute(
            """
            insert into staging.fund_holding_facts
                (fund_id, holding_id, holding_name, report_period, transaction_time,
                 cusip, isin, lei, balance, value_usd, percent_of_net_assets, confidence, raw_ref)
            values (%s, %s, %s, %s, %s, null, %s, null, 1, 100, 5.0, 1.0, 'raw.fetches:0')
            on conflict do nothing
            """,
            ("etf:series:S-TEST-ENRICH", minted, isin, "2026-06-30", _AT, isin),
        )


def _land_openfigi_batch(connection, store) -> None:
    raw_store.insert_json_fetch(
        connection,
        source=DataSource.OPENFIGI,
        source_record_id=f"mapping:{_NVDA_ISIN}",
        payload=[{"data": _NVDA_RECORDS}, {"data": []}],
        fetched_at=_AT,
        metadata={"isins": [_NVDA_ISIN, _FOREIGN_ISIN]},
        store=store,
    )


def test_sweep_repoints_from_landed_batches_without_a_vendor(connection) -> None:
    store = _InMemoryObjectStore()
    _seed_tranche_one(connection)
    _land_openfigi_batch(connection, store)

    outcome = enrich_holding_identities(connection, ticker_cik={"NVDA": 1045810}, now=_AT, store=store)

    assert outcome.unresolved == 2
    assert outcome.reused_from_raw == 2, "both ISINs answer from the landing zone"
    assert outcome.fetched == 0, "no vendor call when raw already has the batch"
    assert outcome.repointed == 1
    assert outcome.unmapped == 1, "the foreign ISIN stays minted, retried next sweep"

    target = er.resolve(connection, "isin", _NVDA_ISIN, as_of=_AT + timedelta(days=1))
    assert target == "issuer:cik:0001045810", f"newest vintage re-points resolution, got {target}"
    assert er.resolve(connection, "ticker", "NVDA", as_of=_AT + timedelta(days=1)) == target
    assert (er.resolve(connection, "isin", _FOREIGN_ISIN, as_of=_AT + timedelta(days=1)) or "").startswith(
        "company:isin:"
    )

    same_as = connection.execute(
        "select to_id from staging.kg_edges where from_id = %s and relation_type = 'same_as'",
        (f"company:isin:{_NVDA_ISIN}",),
    ).fetchall()
    assert same_as == [("issuer:cik:0001045810",)], "immutable facts resolve forward through the merge hop"
    vintage_time = connection.execute(
        "select transaction_time from staging.kg_identifiers where identifier_value = %s and source = 'openfigi'",
        (_NVDA_ISIN,),
    ).fetchone()[0]
    assert vintage_time == _AT, "the vintage carries the supplying batch's own clock (review on #695)"


def test_second_sweep_is_a_no_op(connection) -> None:
    store = _InMemoryObjectStore()
    _seed_tranche_one(connection)
    _land_openfigi_batch(connection, store)
    enrich_holding_identities(connection, ticker_cik={"NVDA": 1045810}, now=_AT, store=store)

    again = enrich_holding_identities(connection, ticker_cik={"NVDA": 1045810}, now=_AT, store=store)

    assert again.repointed == 0, "a re-keyed identity never re-enters the sweep"
    assert again.unresolved == 1 and again.unmapped == 1, "only the still-minted foreign ISIN is considered"
    vintages = connection.execute(
        "select count(*) from staging.kg_identifiers where identifier_value = %s and source = 'openfigi'",
        (_NVDA_ISIN,),
    ).fetchone()[0]
    assert vintages == 1, "no duplicate identifier vintages sprayed by re-runs"


def test_figi_from_raw_skips_ticker_keyed_batches(connection) -> None:
    store = _InMemoryObjectStore()
    raw_store.insert_json_fetch(
        connection,
        source=DataSource.OPENFIGI,
        source_record_id="mapping:ticker-keyed",
        payload=[{"data": _NVDA_RECORDS}],
        fetched_at=_AT,
        metadata={"request_jobs": [{"idType": "TICKER", "idValue": "NVDA"}]},
        store=store,
    )
    mapping, missing = figi_from_raw(connection, [_NVDA_ISIN], store=store)
    assert mapping == {} and missing == [_NVDA_ISIN], "ticker-keyed batches carry no isins metadata"
